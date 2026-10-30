#!/usr/bin/env bash
# In-cluster TLS + PQC scan for MaaS group-test (ODH layout). Modeled on openshift/release tls-scanner-run pod path.
# Requires: oc logged in, pull access to SCANNER_IMAGE, ARTIFACT_DIR set by caller.
set -euo pipefail

SCANNER_NAMESPACE="${TLS_SCANNER_NAMESPACE:-tls-scanner-maas-e2e}"
SCANNER_IMAGE="${TLS_SCANNER_IMAGE:-registry.ci.openshift.org/tls-scanner/tls-scanner:tls-scanner-tool}"
# ODH group-test: controller in opendatahub, maas-api in odh-ai-gateway-infra.
SCAN_NAMESPACE="${TLS_SCAN_NAMESPACE:-opendatahub,odh-ai-gateway-infra}"
COMPONENT_FILTER="${TLS_COMPONENT_FILTER:-maas-api,maas-controller,ai-gateway-operator,ai-gateway-controller}"
# Metrics/webhook ports are declared on pods but often reject TLS probes; validate allowlisted ports only.
TLS_SCAN_PORT_ALLOWLIST="${TLS_SCAN_PORT_ALLOWLIST:-443,8443}"
TLS_REQUIRED_COMPONENTS="${TLS_REQUIRED_COMPONENTS:-maas-api,maas-controller}"
TLS_REQUIRE_PQC="${TLS_REQUIRE_PQC:-true}"
SCANNER_CPU_REQUEST="${TLS_SCANNER_CPU_REQUEST:-500m}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCANNER_CPU_LIMIT="${TLS_SCANNER_CPU_LIMIT:-4}"
SCANNER_MEM_REQUEST="${TLS_SCANNER_MEM_REQUEST:-1Gi}"
SCANNER_MEM_LIMIT="${TLS_SCANNER_MEM_LIMIT:-4Gi}"

if [[ -z "${ARTIFACT_DIR:-}" ]]; then
  echo "ARTIFACT_DIR must be set" >&2
  exit 1
fi
mkdir -p "${ARTIFACT_DIR}"
SCANNER_ARTIFACT_DIR="$(cd "${ARTIFACT_DIR}" && pwd -P)"

cleanup() {
  oc delete namespace "${SCANNER_NAMESPACE}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== TLS PQC scan (Konflux) ==="
echo "Scanner namespace: ${SCANNER_NAMESPACE}"
echo "Scan namespaces: ${SCAN_NAMESPACE}"
echo "Component filter: ${COMPONENT_FILTER}"
echo "Port allowlist (post-validation): ${TLS_SCAN_PORT_ALLOWLIST}"
echo "Required components: ${TLS_REQUIRED_COMPONENTS}"
echo "Image: ${SCANNER_IMAGE}"

oc delete namespace "${SCANNER_NAMESPACE}" --ignore-not-found --wait=true --timeout=120s || true
oc create namespace "${SCANNER_NAMESPACE}"

if oc get secret pull-secret -n openshift-config >/dev/null 2>&1; then
  oc get secret pull-secret -n openshift-config -o yaml \
    | sed "s/namespace: .*/namespace: ${SCANNER_NAMESPACE}/" \
    | oc apply -n "${SCANNER_NAMESPACE}" -f -
  oc secrets link default pull-secret --for=pull -n "${SCANNER_NAMESPACE}" 2>/dev/null || true
fi

oc adm policy add-cluster-role-to-user cluster-admin -z default -n "${SCANNER_NAMESPACE}"
echo "Waiting for RBAC to propagate..."
sleep 10

SCANNER_ARGS="--all-pods --namespace-filter ${SCAN_NAMESPACE}"
SCANNER_ARGS="${SCANNER_ARGS} --component-filter=${COMPONENT_FILTER} --pqc-check"

cat <<EOF | oc apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: tls-scanner
  namespace: ${SCANNER_NAMESPACE}
  annotations:
    openshift.io/required-scc: restricted-v2
  labels:
    app: tls-scanner
spec:
  serviceAccountName: default
  restartPolicy: Never
  hostNetwork: false
  hostPID: false
  containers:
  - name: scanner
    image: ${SCANNER_IMAGE}
    command:
    - /bin/bash
    - -c
    - |
      mkdir -p /results
      /usr/local/bin/tls-scanner -j 4 ${SCANNER_ARGS} \
        --json-file /results/results.json \
        --csv-file /results/results.csv \
        --junit-file /results/junit_tls_scan.xml \
        --log-file /results/scan.log 2>&1 | tee /results/output.log
      SCAN_EXIT_CODE=\${PIPESTATUS[0]}
      echo "Scan complete. Exit code: \${SCAN_EXIT_CODE}" | tee -a /results/output.log
      touch /results/scan.done
      sleep 120
      exit \${SCAN_EXIT_CODE}
    resources:
      requests:
        cpu: "${SCANNER_CPU_REQUEST}"
        memory: ${SCANNER_MEM_REQUEST}
      limits:
        cpu: "${SCANNER_CPU_LIMIT}"
        memory: ${SCANNER_MEM_LIMIT}
    securityContext:
      allowPrivilegeEscalation: false
      runAsNonRoot: true
      capabilities:
        drop:
        - ALL
      seccompProfile:
        type: RuntimeDefault
    volumeMounts:
    - name: results
      mountPath: /results
  volumes:
  - name: results
    emptyDir: {}
EOF

oc wait --for=condition=Ready pod/tls-scanner -n "${SCANNER_NAMESPACE}" --timeout=10m

oc logs -f pod/tls-scanner -n "${SCANNER_NAMESPACE}" &
LOGS_PID=$!

while true; do
  phase="$(oc get pod tls-scanner -n "${SCANNER_NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo Unknown)"
  if oc exec pod/tls-scanner -n "${SCANNER_NAMESPACE}" -- test -f /results/scan.done 2>/dev/null; then
    break
  fi
  if [[ "${phase}" == "Succeeded" || "${phase}" == "Failed" ]]; then
    break
  fi
  sleep 15
done

oc cp "${SCANNER_NAMESPACE}/tls-scanner:/results/." "${SCANNER_ARTIFACT_DIR}/" || {
  echo "Failed to copy scanner artifacts" >&2
  exit 1
}

wait "${LOGS_PID}" 2>/dev/null || true

for f in results.json results.csv; do
  if [[ ! -s "${SCANNER_ARTIFACT_DIR}/${f}" ]]; then
    echo "Missing or empty artifact: ${f}" >&2
    exit 1
  fi
done

pod_exit="$(oc get pod/tls-scanner -n "${SCANNER_NAMESPACE}" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || echo "")"
if [[ -n "${pod_exit}" && "${pod_exit}" != "0" ]]; then
  echo "TLS scanner pod exited with code ${pod_exit}" >&2
  exit "${pod_exit}"
fi

export TLS_SCAN_PORT_ALLOWLIST TLS_REQUIRED_COMPONENTS TLS_REQUIRE_PQC
if ! python3 "${SCRIPT_DIR}/validate-tls-pqc-results.py"; then
  echo "TLS/PQC artifact validation failed (see above)" >&2
  exit 1
fi

echo "TLS PQC scan artifacts: ${SCANNER_ARTIFACT_DIR}"
cleanup
trap - EXIT
