#!/bin/bash
# Merge quay.io/rhoai credentials into the EaaS cluster's global pull secret,
# pre-create an imagePullSecret in openshift-marketplace for OLM pods, and
# register an additional-pull-secret in kube-system for HyperShift node sync.
#
# Why all three are needed:
#  - Global pull secret (openshift-config/pull-secret): the only supported auth
#    mechanism for IDMS mirror registries per OpenShift docs. In HyperShift this
#    normally requires node replacement to propagate; we bypass that with HCCO below.
#  - additional-pull-secret (kube-system): triggers HyperShift's Hosted Cluster
#    Config Operator (HCCO) to deploy the global-pull-secret-syncer DaemonSet,
#    which updates /var/lib/kubelet/config.json on every node and restarts kubelet.
#    This makes quay.io/rhoai credentials available to CRI-O at the system level
#    *without* full node replacement, fixing the CRI-O < 1.34 bug where pod-level
#    imagePullSecrets are not forwarded to IDMS mirror registry pulls.
#  - rhoai-quay-pull (openshift-marketplace): SA imagePullSecret for direct pulls
#    (CatalogSource FBC image, etc.) which are NOT via IDMS and work fine with
#    pod-level secrets.
#
# ** Internal Tekton pipeline step — not meant to be called directly. **
# To trigger the test from your laptop use:  integration-tests/olminstall/run-olminstall.sh
#
# In Tekton the quay secret is volume-mounted at /var/secret/quay/.dockerconfigjson.

set -euo pipefail

QUAY_SECRET="/var/secret/quay/.dockerconfigjson"
if [ ! -f "$QUAY_SECRET" ]; then
  echo "❌ Quay secret not mounted at ${QUAY_SECRET}"
  exit 1
fi

QUAY=$(cat "$QUAY_SECRET")

QUAY_AUTH=$(echo "$QUAY" | jq -r '
  .auths["quay.io"].auth //
  .auths["quay.io/rhoai"].auth //
  .auths["quay.io/rhoai/rhoai-fbc-fragment"].auth //
  (.auths | to_entries | map(select(.key | startswith("quay.io/rhoai/"))) | first | .value.auth) //
  empty')
if [ -z "$QUAY_AUTH" ]; then
  echo "❌ No quay.io/rhoai auth token found in ${QUAY_SECRET}"
  exit 1
fi

QUAY=$(echo "$QUAY" | jq --arg a "$QUAY_AUTH" '.auths["quay.io"] = {"auth": $a}')

echo "Patching cluster global pull secret with quay.io/rhoai credentials..."
EXISTING=$(oc get secret/pull-secret -n openshift-config \
  -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d)
MERGED=$(jq -s '.[0] * {auths: ((.[0].auths // {}) * (.[1].auths // {}))}' \
  <(echo "$EXISTING") <(echo "$QUAY"))
if [ -z "$MERGED" ] || [ "$MERGED" = "null" ]; then
  echo "❌ jq merge produced empty result"
  exit 1
fi
oc patch secret/pull-secret -n openshift-config \
  --type=merge \
  -p "{\"data\":{\".dockerconfigjson\":\"$(echo -n "$MERGED" | base64 -w0)\"}}"
echo "✓ Global pull secret patched"

# Trigger HCCO's global-pull-secret-syncer: create additional-pull-secret in kube-system.
# HCCO detects this and deploys a DaemonSet that writes /var/lib/kubelet/config.json on
# each node and restarts kubelet — making credentials available to CRI-O for all pulls
# (including IDMS mirrors) without full node replacement.
# Use quay.io/rhoai/* namespace-specific keys to avoid conflicting with the cluster's
# existing bare quay.io entry (HCCO keeps the original entry on conflict).
echo "Creating additional-pull-secret in kube-system (triggers HyperShift HCCO node sync)..."
RHOAI_CREDS=$(echo "$QUAY" | jq '{
  auths: (.auths | to_entries
    | map(select(.key | startswith("quay.io/rhoai")))
    | from_entries)
}')
# Ensure a quay.io/rhoai catch-all is present (in case only per-repo keys exist).
if [ -n "$QUAY_AUTH" ]; then
  RHOAI_CREDS=$(echo "$RHOAI_CREDS" | \
    jq --arg a "$QUAY_AUTH" '.auths["quay.io/rhoai"] |= . // {"auth": $a}')
fi
oc create secret generic additional-pull-secret \
  -n kube-system \
  --from-literal=.dockerconfigjson="$RHOAI_CREDS" \
  --type=kubernetes.io/dockerconfigjson \
  --dry-run=client -o yaml | oc apply -f -
echo "✓ additional-pull-secret created in kube-system"

echo "Creating rhoai-quay-pull imagePullSecret in openshift-marketplace for OLM SA-level pulls..."
oc create secret generic rhoai-quay-pull \
  -n openshift-marketplace \
  --from-literal=.dockerconfigjson="$QUAY" \
  --type=kubernetes.io/dockerconfigjson \
  --dry-run=client -o yaml | oc apply -f -

for SA in $(oc get sa -n openshift-marketplace --no-headers \
    -o custom-columns=':metadata.name' 2>/dev/null); do
  oc secrets link "$SA" rhoai-quay-pull -n openshift-marketplace --for=pull 2>/dev/null || true
done
echo "✓ rhoai-quay-pull linked to all SAs in openshift-marketplace"
