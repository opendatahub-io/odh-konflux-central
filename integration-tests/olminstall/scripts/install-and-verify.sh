#!/bin/bash
# Install the RHOAI operator via OLM from a Konflux FBCF image and verify the CSV
# reaches Succeeded status.
#
# ** Internal Tekton pipeline step — not meant to be called directly. **
# To trigger the test from your laptop use:  integration-tests/olminstall/run-test.sh
#
# Strategy: create CatalogSource with securityContextConfig=legacy first so OLM's
# gRPC client connects in plain mode from the start, avoiding "http2: frame too
# large" TLS mismatches when Subscription is created before the mode is set.
#
# Auth strategy for CRI-O < 1.34 (cri-o/cri-o#4941):
#   CRI-O does NOT forward pod imagePullSecrets to IDMS mirror registries.
#   OpenShift docs explicitly state that for IDMS mirrors, only the cluster-wide
#   global pull secret is supported.  In HyperShift, updating the global pull
#   secret normally triggers full node replacement (slow).
#
#   Fix: patch-cluster-pull-secret.sh creates additional-pull-secret in kube-system.
#   HyperShift's Hosted Cluster Config Operator (HCCO) detects it and deploys a
#   global-pull-secret-syncer DaemonSet that updates /var/lib/kubelet/config.json
#   on every node and restarts kubelet — without full node replacement.  We wait
#   for that DaemonSet here before creating the Subscription.
#
# Required env vars (injected by the pipeline):
#   KUBECONFIG            FBCF_IMAGE          UPDATE_CHANNEL
#   OPERATOR_NAMESPACE    OPERATOR_NAME
#   INSTALL_STATUS_PATH   OPERATOR_VERSION_PATH

set -eo pipefail

fail() { echo -n "FAILED" > "${INSTALL_STATUS_PATH}"; exit 1; }

echo "========================================="
echo " ODH/RHOAI Operator Installation"
echo " FBCF:      ${FBCF_IMAGE}"
echo " Channel:   ${UPDATE_CHANNEL}"
echo " Operator:  ${OPERATOR_NAME} -> ${OPERATOR_NAMESPACE}"
echo "========================================="

oc version || { echo "❌ Cannot connect to cluster"; fail; }

oc create namespace "${OPERATOR_NAMESPACE}" --dry-run=client -o yaml | oc apply -f -

echo "Creating CatalogSource (legacy security context)..."
oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1alpha1
kind: CatalogSource
metadata:
  name: rhoai-catalog-dev
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

echo "Waiting for OLM to create the rhoai-catalog-dev ServiceAccount (up to 2m)..."
SA_DEADLINE=$(($(date +%s) + 120))
while [ "$(date +%s)" -lt "$SA_DEADLINE" ]; do
  oc get sa rhoai-catalog-dev -n openshift-marketplace &>/dev/null && break
  sleep 5
done
oc secrets link rhoai-catalog-dev rhoai-quay-pull -n openshift-marketplace --for=pull 2>/dev/null || true

echo "Restarting CatalogSource pod to pick up the rhoai-quay-pull SA secret..."
oc delete pod -n openshift-marketplace -l olm.catalogSource=rhoai-catalog-dev \
  --ignore-not-found=true
sleep 30

echo "Waiting for CatalogSource to be READY (up to 15m)..."
CS_STATUS="" ITER=0 DEADLINE=$(($(date +%s) + 900))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  CS_STATUS=$(oc get catalogsource rhoai-catalog-dev -n openshift-marketplace \
    -o jsonpath='{.status.connectionState.lastObservedState}' 2>/dev/null || true)
  [ "$CS_STATUS" = "READY" ] && { echo "✓ CatalogSource READY"; break; }
  ITER=$((ITER + 1))
  echo "  CS state: ${CS_STATUS:-unknown} (iter ${ITER})"
  if [ $((ITER % 4)) -eq 0 ]; then
    CS_POD=$(oc get pods -n openshift-marketplace -l olm.catalogSource=rhoai-catalog-dev \
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
  oc describe catalogsource rhoai-catalog-dev -n openshift-marketplace || true
  CS_POD=$(oc get pods -n openshift-marketplace -l olm.catalogSource=rhoai-catalog-dev \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [ -n "$CS_POD" ] && oc describe pod "$CS_POD" -n openshift-marketplace 2>/dev/null | tail -30 || true
  fail
fi

echo "Copying rhoai-quay-pull to ${OPERATOR_NAMESPACE} and linking to all SAs..."
oc get secret rhoai-quay-pull -n openshift-marketplace -o yaml \
  | sed "s/namespace: openshift-marketplace/namespace: ${OPERATOR_NAMESPACE}/" \
  | oc apply -f - || true
for SA in $(oc get sa -n "${OPERATOR_NAMESPACE}" --no-headers \
    -o custom-columns=':metadata.name' 2>/dev/null); do
  oc secrets link "$SA" rhoai-quay-pull -n "${OPERATOR_NAMESPACE}" --for=pull 2>/dev/null || true
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

echo "Creating OperatorGroup and Subscription..."
oc apply -f - <<EOF
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: rhoai-operator-dev
  namespace: ${OPERATOR_NAMESPACE}
spec: {}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: rhoai-operator-dev
  namespace: ${OPERATOR_NAMESPACE}
spec:
  channel: ${UPDATE_CHANNEL}
  name: ${OPERATOR_NAME}
  source: rhoai-catalog-dev
  sourceNamespace: openshift-marketplace
  installPlanApproval: Manual
EOF
echo "✓ OLM resources created"

echo "Waiting for InstallPlan in ${OPERATOR_NAMESPACE} (up to 10m)..."
PLAN="" ITER=0 DEADLINE=$(($(date +%s) + 600))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  PLAN=$(oc get installplan -n "${OPERATOR_NAMESPACE}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "$PLAN" ]; then
    echo "Found InstallPlan: $PLAN"
    oc patch installplan "${PLAN}" -n "${OPERATOR_NAMESPACE}" \
      --type=merge -p '{"spec":{"approved":true}}' 2>/dev/null || true
    break
  fi
  ITER=$((ITER + 1))
  echo "  waiting for InstallPlan... (iter ${ITER})"
  if [ $((ITER % 3)) -eq 0 ]; then
    UNPACK_JOB=$(oc get jobs -n openshift-marketplace --no-headers 2>/dev/null \
      | grep -v "catalog-operator\|rhoai-catalog" | awk '{print $1}' | head -1 || true)
    if [ -n "$UNPACK_JOB" ]; then
      UNPACK_POD=$(oc get pods -n openshift-marketplace -l "job-name=${UNPACK_JOB}" \
        --no-headers 2>/dev/null | awk '{print $1}' | head -1 || true)
      if [ -n "$UNPACK_POD" ]; then
        echo "  [diag] unpack job=${UNPACK_JOB} pod=${UNPACK_POD}:"
        oc get pod "$UNPACK_POD" -n openshift-marketplace 2>/dev/null || true
        oc get pod "$UNPACK_POD" -n openshift-marketplace \
          -o jsonpath='sa={.spec.serviceAccountName} imagePullSecrets={.spec.imagePullSecrets}{"\n"}' \
          2>/dev/null || true
        oc get events -n openshift-marketplace \
          --field-selector "involvedObject.name=${UNPACK_POD}" 2>/dev/null | tail -3 || true
      fi
    fi
  fi
  sleep 15
done

if [ -z "$PLAN" ]; then
  echo "❌ No InstallPlan after timeout"
  oc get sub,catalogsource,installplan -n "${OPERATOR_NAMESPACE}" || true
  oc describe sub -n "${OPERATOR_NAMESPACE}" || true
  oc get jobs -n openshift-marketplace 2>/dev/null || true
  UNPACK_POD=$(oc get pods -n openshift-marketplace \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
    | tr ' ' '\n' | grep -i unpack | head -1 || true)
  [ -n "$UNPACK_POD" ] && oc describe pod "$UNPACK_POD" -n openshift-marketplace 2>/dev/null | tail -40 || true
  fail
fi

echo "Waiting for CSV to reach Succeeded in ${OPERATOR_NAMESPACE} (up to 15m)..."
CSV_NAME=""
if ! oc wait csv -n "${OPERATOR_NAMESPACE}" \
    -l "operators.coreos.com/${OPERATOR_NAME}.${OPERATOR_NAMESPACE}=" \
    --for=jsonpath='{.status.phase}'=Succeeded --timeout=15m 2>/dev/null; then
  CSV_NAME=$(oc get csv -n "${OPERATOR_NAMESPACE}" \
    -o jsonpath='{.items[?(@.spec.displayName=="Red Hat OpenShift AI")].metadata.name}' \
    2>/dev/null | awk '{print $1}')
  [ -z "$CSV_NAME" ] && CSV_NAME=$(oc get csv -n "${OPERATOR_NAMESPACE}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -z "$CSV_NAME" ]; then
    echo "❌ No CSV found in ${OPERATOR_NAMESPACE}"
    oc get sub,csv,installplan -n "${OPERATOR_NAMESPACE}" || true
    fail
  fi
  echo "Retrying wait on CSV: ${CSV_NAME}"
  if ! oc wait csv "${CSV_NAME}" -n "${OPERATOR_NAMESPACE}" \
      --for=jsonpath='{.status.phase}'=Succeeded --timeout=15m; then
    echo "❌ CSV ${CSV_NAME} did not reach Succeeded"
    oc get csv "${CSV_NAME}" -n "${OPERATOR_NAMESPACE}" -o yaml | tail -40 || true
    fail
  fi
fi

CSV_VERSION=$(oc get csv -n "${OPERATOR_NAMESPACE}" \
  -o jsonpath="{.items[?(@.spec.displayName=='Red Hat OpenShift AI')].spec.version}" \
  2>/dev/null || echo "unknown")
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
echo -n "SUCCESS" > "${INSTALL_STATUS_PATH}"
