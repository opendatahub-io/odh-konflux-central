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
# To trigger the test from your laptop use:  integration-tests/olminstall/run-test.sh
#
# In Tekton the quay secret is volume-mounted at /var/secret/quay/.dockerconfigjson.

set -eo pipefail

QUAY=$(cat /var/secret/quay/.dockerconfigjson)

# Extract an auth token from any quay.io/rhoai/* key as the wildcard quay.io auth.
QUAY_AUTH=$(echo "$QUAY" | jq -r '
  .auths["quay.io"].auth //
  .auths["quay.io/rhoai"].auth //
  .auths["quay.io/rhoai/rhoai-fbc-fragment"].auth //
  (.auths | to_entries | map(select(.key | startswith("quay.io/rhoai/"))) | first | .value.auth) //
  empty')

# Add bare quay.io entry to cover any quay.io/rhoai/* pull.
if [ -n "$QUAY_AUTH" ]; then
  QUAY=$(echo "$QUAY" | jq --arg a "$QUAY_AUTH" '.auths["quay.io"] = {"auth": $a}')
fi

echo "Patching cluster global pull secret with quay.io/rhoai credentials..."
EXISTING=$(oc get secret/pull-secret -n openshift-config \
  -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d)
MERGED=$(jq -s '.[0].auths * .[1].auths | {auths: .}' <(echo "$EXISTING") <(echo "$QUAY"))
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
  --from-literal=.dockerconfigjson="$(echo "$RHOAI_CREDS")" \
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
