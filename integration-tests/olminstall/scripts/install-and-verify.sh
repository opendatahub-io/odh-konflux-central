#!/bin/bash
# Install the RHOAI operator via OLM from a Konflux FBCF image and verify the CSV
# reaches Succeeded status.
#
# ** Internal Tekton pipeline step — not meant to be called directly. **
# To trigger the test from your laptop use:  integration-tests/olminstall/run-olminstall.sh
#
# OLM install manifests and wait/approve utilities are sourced from
# https://gitlab.cee.redhat.com/data-hub/olminstall (cloned to ${OLMINSTALL_DIR}).
# This script owns only the FBCF-specific steps that olminstall doesn't cover:
#   - CatalogSource creation pointing to the Konflux FBCF image
#   - SA-level pull-secret linking for OLM pods
#   - HyperShift HCCO global-pull-secret-syncer wait (cri-o/cri-o#4941)
#
# Required env vars (injected by the pipeline):
#   KUBECONFIG            FBCF_IMAGE          UPDATE_CHANNEL
#   OPERATOR_NAMESPACE    OPERATOR_NAME       OLMINSTALL_DIR
#   INSTALL_STATUS_PATH   OPERATOR_VERSION_PATH
# Optional env vars:
#   OLMINSTALL_CATALOG_NAME  CatalogSource name passed to olminstall (default: rhoai-catalog-dev)

set -euo pipefail

fail() {
  local msg="${1:-}"
  [[ -n "${msg}" ]] && echo "${msg}"
  if [[ -n "${INSTALL_STATUS_PATH:-}" ]]; then
    echo -n "FAILED" > "${INSTALL_STATUS_PATH}" || true
  fi
  exit 1
}

require_env() {
  local var_name="$1"
  if [[ -z "${!var_name:-}" ]]; then
    fail "❌ Required environment variable is missing: ${var_name}"
  fi
}

require_env "INSTALL_STATUS_PATH"
require_env "OPERATOR_NAMESPACE"
require_env "OPERATOR_NAME"
require_env "UPDATE_CHANNEL"
require_env "FBCF_IMAGE"
require_env "OLMINSTALL_DIR"
require_env "OPERATOR_VERSION_PATH"
OLMINSTALL_CATALOG_NAME="${OLMINSTALL_CATALOG_NAME:-rhoai-catalog-dev}"
QUAY_PULL_SECRET_NAME="${QUAY_PULL_SECRET_NAME:-rhoai-quay-pull}"

trap 'fail "❌ Unexpected error on line ${LINENO}"' ERR
trap 'fail "❌ Interrupted"' INT TERM HUP

echo "========================================="
echo " ODH/RHOAI Operator Installation"
echo " FBCF:      ${FBCF_IMAGE}"
echo " Channel:   ${UPDATE_CHANNEL}"
echo " Operator:  ${OPERATOR_NAME} -> ${OPERATOR_NAMESPACE}"
echo "========================================="

oc version || { echo "❌ Cannot connect to cluster"; fail; }

oc create namespace "${OPERATOR_NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

# Validate FBCF_IMAGE looks like a container image reference before embedding
# it in YAML to prevent accidental YAML injection from a malformed snapshot value.
if ! echo "${FBCF_IMAGE}" | grep -qE '^[A-Za-z0-9./_:@-]+$'; then
  fail "❌ FBCF_IMAGE contains unexpected characters: ${FBCF_IMAGE}"
fi

echo "Creating CatalogSource (legacy security context)..."
oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: ${OLMINSTALL_CATALOG_NAME}
  namespace: openshift-marketplace
spec:
  sourceType: grpc
  image: ${FBCF_IMAGE}
  displayName: RHOAI Dev Catalog
  publisher: Red Hat
  updateStrategy:
    registryPoll:
      interval: 30m
  grpcPodConfig:
    securityContextConfig: legacy
EOF

echo "Waiting for OLM to create the ${OLMINSTALL_CATALOG_NAME} ServiceAccount (up to 2m)..."
SA_DEADLINE=$(($(date +%s) + 120))
while [ "$(date +%s)" -lt "$SA_DEADLINE" ]; do
  oc get sa "${OLMINSTALL_CATALOG_NAME}" -n openshift-marketplace &>/dev/null && break
  sleep 5
done
if ! oc secrets link "${OLMINSTALL_CATALOG_NAME}" "${QUAY_PULL_SECRET_NAME}" -n openshift-marketplace --for=pull 2>/dev/null; then
  echo "⚠ Could not link ${QUAY_PULL_SECRET_NAME} to ${OLMINSTALL_CATALOG_NAME} SA (SA may not exist yet — non-fatal)"
fi

echo "Restarting CatalogSource pod to pick up the ${QUAY_PULL_SECRET_NAME} SA secret..."
oc delete pod -n openshift-marketplace -l "olm.catalogSource=${OLMINSTALL_CATALOG_NAME}" \
  --ignore-not-found=true
# Wait for the deleted pod to disappear rather than a fixed sleep; the 15-minute
# READY poll that follows handles the "replacement not yet running" case.
oc wait --for=delete pod -n openshift-marketplace \
  -l "olm.catalogSource=${OLMINSTALL_CATALOG_NAME}" --timeout=60s 2>/dev/null || true

echo "Waiting for CatalogSource to be READY (up to 15m)..."
CS_STATUS="" ITER=0 DEADLINE=$(($(date +%s) + 900))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  CS_STATUS=$(oc get catalogsource "${OLMINSTALL_CATALOG_NAME}" -n openshift-marketplace \
    -o jsonpath='{.status.connectionState.lastObservedState}' 2>/dev/null || true)
  [ "$CS_STATUS" = "READY" ] && { echo "✓ CatalogSource READY"; break; }
  ITER=$((ITER + 1))
  echo "  CS state: ${CS_STATUS:-unknown} (iter ${ITER})"
  if [ $((ITER % 4)) -eq 0 ]; then
    CS_POD=$(oc get pods -n openshift-marketplace -l "olm.catalogSource=${OLMINSTALL_CATALOG_NAME}" \
      --no-headers -o custom-columns=':metadata.name' 2>/dev/null | head -1 || true)
    if [ -n "$CS_POD" ]; then
      oc get pod "$CS_POD" -n openshift-marketplace 2>/dev/null || true
      oc get events -n openshift-marketplace \
        --field-selector "involvedObject.name=${CS_POD}" 2>/dev/null | tail -3 || true
    else
      echo "  no CatalogSource pod yet"
      oc get pods -n openshift-marketplace --no-headers 2>/dev/null || true
    fi
  fi
  sleep 15
done

if [ "$CS_STATUS" != "READY" ]; then
  echo "❌ CatalogSource not READY after timeout"
  oc describe catalogsource "${OLMINSTALL_CATALOG_NAME}" -n openshift-marketplace || true
  CS_POD=$(oc get pods -n openshift-marketplace -l "olm.catalogSource=${OLMINSTALL_CATALOG_NAME}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [ -n "$CS_POD" ] && oc describe pod "$CS_POD" -n openshift-marketplace 2>/dev/null | tail -30 || true
  fail
fi

echo "Copying ${QUAY_PULL_SECRET_NAME} to ${OPERATOR_NAMESPACE} and linking to all SAs..."
if ! oc get secret "${QUAY_PULL_SECRET_NAME}" -n openshift-marketplace -o json \
    | jq --arg ns "${OPERATOR_NAMESPACE}" '
        del(
          .metadata.uid,
          .metadata.resourceVersion,
          .metadata.creationTimestamp,
          .metadata.managedFields,
          .metadata.ownerReferences,
          .metadata.selfLink,
          .metadata.generation,
          .metadata.annotations."kubectl.kubernetes.io/last-applied-configuration"
        )
        | .metadata.namespace = $ns
      ' \
    | oc apply -f -; then
  echo "⚠ Failed to copy ${QUAY_PULL_SECRET_NAME} to ${OPERATOR_NAMESPACE} — OLM SA-level pulls may fail"
fi
for SA in $(oc get sa -n "${OPERATOR_NAMESPACE}" --no-headers \
    -o custom-columns=':metadata.name' 2>/dev/null); do
  oc secrets link "$SA" "${QUAY_PULL_SECRET_NAME}" -n "${OPERATOR_NAMESPACE}" --for=pull 2>/dev/null || true
done

# Wait for HyperShift HCCO to sync quay.io/rhoai credentials to node kubelet config.
#
# patch-cluster-pull-secret.sh created additional-pull-secret in kube-system.
# The Hosted Cluster Config Operator (HCCO) detects it and automatically deploys
# global-pull-secret-syncer — a DaemonSet that updates /var/lib/kubelet/config.json
# on each node and restarts kubelet.  This is the official HyperShift mechanism for
# propagating pull-secret changes without full node replacement, and it makes the
# credentials available to CRI-O for ALL pulls including IDMS mirrors.
# HCCO deploys global-pull-secret-syncer after detecting additional-pull-secret in kube-system.
# It typically appears within 1-2 min; the CatalogSource wait above gives it a head-start.
echo "Waiting for HyperShift HCCO to sync quay.io/rhoai credentials to all nodes (up to 5m)..."
SYNC_DESIRED=0
for i in $(seq 1 24); do
  if oc get daemonset global-pull-secret-syncer -n kube-system &>/dev/null; then
    SYNC_DESIRED=$(oc get ds global-pull-secret-syncer -n kube-system \
      -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || echo "0")
    [ "${SYNC_DESIRED:-0}" -gt "0" ] && break
  fi
  echo "  waiting for global-pull-secret-syncer DaemonSet... (check ${i}/24)"
  sleep 5
done

if [ "${SYNC_DESIRED:-0}" -eq "0" ]; then
  echo "⚠ global-pull-secret-syncer DaemonSet not found after 2m — HCCO feature may not be"
  echo "  available on this cluster version. Proceeding; bundle-unpack may fail with ErrImagePull."
else
  echo "  global-pull-secret-syncer desired=${SYNC_DESIRED}"
  SYNC_DEADLINE=$(($(date +%s) + 180))
  SYNC_READY=0
  while [ "$(date +%s)" -lt "$SYNC_DEADLINE" ]; do
    SYNC_READY=$(oc get ds global-pull-secret-syncer -n kube-system \
      -o jsonpath='{.status.numberReady}' 2>/dev/null || echo "0")
    echo "  nodes synced: ${SYNC_READY:-0}/${SYNC_DESIRED}"
    if [ "${SYNC_READY:-0}" -ge "${SYNC_DESIRED:-1}" ]; then
      echo "✓ quay.io/rhoai credentials synced to all ${SYNC_DESIRED} nodes"
      break
    fi
    sleep 10
  done
  if [ "${SYNC_READY:-0}" -lt "${SYNC_DESIRED:-1}" ]; then
    echo "⚠ Syncer incomplete after 3m (${SYNC_READY:-0}/${SYNC_DESIRED} nodes) — proceeding"
    oc get pods -n kube-system -l name=global-pull-secret-syncer \
      --no-headers 2>/dev/null | head -5 || true
  fi
fi

# ── OLM install via data-hub/olminstall (same call pattern as Jenkins) ────────
#
# Fix olminstall's oc_wait_for_csv default namespace (upstream bug; only affects our copy)
sed -i 's|local namespace="${2:-default}"|local namespace="${2:-'"${OPERATOR_NAMESPACE}"'}"|' \
  "${OLMINSTALL_DIR}/utils/oc_wait.sh"
# Resolve olminstall manifest and operator arg.
# olminstall's install-operator.sh requires resources/install-<name>.yaml to exist.
# If no operator-specific manifest exists, fall back to rhods-operator and pass that
# name to install-operator.sh (the manifest is namespace-patched below).
OLMINSTALL_OPERATOR="${OPERATOR_NAME}"
OLMINSTALL_MANIFEST="${OLMINSTALL_DIR}/resources/install-${OPERATOR_NAME}.yaml"
if [[ ! -f "${OLMINSTALL_MANIFEST}" ]]; then
  FALLBACK_MANIFEST="${OLMINSTALL_DIR}/resources/install-rhods-operator.yaml"
  if [[ -f "${FALLBACK_MANIFEST}" ]]; then
    echo "⚠ Manifest install-${OPERATOR_NAME}.yaml not found — using rhods-operator manifest"
    OLMINSTALL_MANIFEST="${FALLBACK_MANIFEST}"
    OLMINSTALL_OPERATOR="rhods-operator"
  else
    fail "❌ No olminstall manifest found for operator ${OPERATOR_NAME}"
  fi
fi

# Patch only the namespace field in the manifest, not every occurrence of the
# string (which would corrupt Subscription/OperatorGroup names and label values).
sed -i "s|^\(\s*namespace:\s*\)redhat-ods-operator\s*$|\1${OPERATOR_NAMESPACE}|" "${OLMINSTALL_MANIFEST}"

echo "Running olminstall (./install-operator.sh ${OLMINSTALL_OPERATOR} ${UPDATE_CHANNEL} ${OLMINSTALL_CATALOG_NAME})..."
(cd "${OLMINSTALL_DIR}" && \
  ./install-operator.sh "${OLMINSTALL_OPERATOR}" "${UPDATE_CHANNEL}" "${OLMINSTALL_CATALOG_NAME}") || {
  echo "❌ olminstall install-operator.sh failed"
  oc get sub,csv,installplan -n "${OPERATOR_NAMESPACE}" || true
  oc describe sub -n "${OPERATOR_NAMESPACE}" || true
  fail
}

# Match the CSV olminstall actually installed (OLMINSTALL_OPERATOR may be rhods-operator
# when OPERATOR_NAME is opendatahub-operator and we use the rhods-operator manifest fallback).
CSV_VERSION=$(oc get csv -n "${OPERATOR_NAMESPACE}" -o json 2>/dev/null \
  | jq -r --arg op "${OLMINSTALL_OPERATOR}" '
      [ .items[]
        | select(.status.phase == "Succeeded")
        | select((.metadata.name // "" | startswith($op))
                 or (.spec.displayName // "" | test($op; "i"))) ]
      | first | .spec.version // empty')
if [[ -z "${CSV_VERSION}" || "${CSV_VERSION}" == "unknown" ]]; then
  echo "❌ No CSV reached Succeeded phase in namespace ${OPERATOR_NAMESPACE}"
  oc get csv -n "${OPERATOR_NAMESPACE}" || true
  fail
fi
echo -n "${CSV_VERSION}" > "${OPERATOR_VERSION_PATH}"

echo ""
echo "========================================="
echo " Installation Results"
echo "========================================="
echo " Operator version : ${CSV_VERSION}"
echo " Namespace        : ${OPERATOR_NAMESPACE}"
echo " Channel          : ${UPDATE_CHANNEL}"
echo " FBCF image       : ${FBCF_IMAGE}"
echo "-----------------------------------------"
echo " CSV status:"
oc get csv -n "${OPERATOR_NAMESPACE}" \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,VERSION:.spec.version' \
  2>/dev/null || true
echo "-----------------------------------------"
echo " Operator deployment:"
oc get deployment -n "${OPERATOR_NAMESPACE}" \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas,IMAGE:.spec.template.spec.containers[0].image' \
  2>/dev/null || true
echo "-----------------------------------------"
echo " Installed CRDs (rhoai):"
oc get crd 2>/dev/null | grep -iE "opendatahub|datasciencecluster|rhoai|kfdef" | awk '{print "  "$1}' || true
echo "========================================="
echo "✅ Installation complete — operator version: ${CSV_VERSION}"
trap - ERR INT TERM HUP
echo -n "SUCCESS" > "${INSTALL_STATUS_PATH}"
