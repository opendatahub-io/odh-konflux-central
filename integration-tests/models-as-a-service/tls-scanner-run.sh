#!/bin/bash
# Originally based on openshift/release ci-operator/step-registry/tls/scanner/run/tls-scanner-run-commands.sh
# (see steps.ci.openshift.org tls-scanner-run). This copy is **heavily diverged** for Konflux/MaaS
# (dedicated SA, verified cluster-admin removal, bounded oc calls, RBAC classifier, pipeline hooks).
# Do **not** treat it as a drop-in sync from openshift/release — port upstream changes manually and
# re-test. Longer term, consider upstreaming the SA + cleanup hardening to openshift/release.
#
# Konflux: same cluster as e2e after smoke. Set PULL_SPEC_TLS_SCANNER_TOOL, PQC_CHECK,
# SCAN_NAMESPACE, COMPONENT_FILTER before invoking.
#
# MaaS: scanner pod uses a dedicated ServiceAccount (SCANNER_SERVICE_ACCOUNT_NAME, default
# tls-scanner) for cluster-admin — not the namespace default SA. Bindings are removed in cleanup().
set -o nounset
set -o errexit
set -o pipefail

# Clusters behind a bastion (e.g. baremetal dev-scripts) are only reachable
# through the proxy configured by the install steps.
if [[ -n "${SHARED_DIR:-}" ]] && test -f "${SHARED_DIR}/proxy-conf.sh"; then
  # shellcheck disable=SC1090
  source "${SHARED_DIR}/proxy-conf.sh"
fi

# Classify a ClusterRoleBinding List on stdin for system:serviceaccount:<ns>:<sa>.
# 0 present, 10 absent (well-formed list, no match), 11 JSON, 12 bad list envelope,
# 14 malformed binding/subject, 13 unexpected.
# Omitted subjects or "subjects": [] grants nobody. "subjects": null is not a list.
_classify_crb_json() {
  local ns="$1"
  local sa="$2"
  python3 -c '
import json, sys
ns, sa = sys.argv[1], sys.argv[2]
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(11)

def nonempty_str(value):
    return isinstance(value, str) and value != ""

try:
    if not isinstance(data, dict) or "items" not in data or not isinstance(data["items"], list):
        sys.exit(12)
    matched = False
    for item in data["items"]:
        if not isinstance(item, dict):
            sys.exit(14)
        role_ref = item.get("roleRef")
        if not isinstance(role_ref, dict):
            sys.exit(14)
        api_group = role_ref.get("apiGroup")
        role_kind = role_ref.get("kind")
        role_name = role_ref.get("name")
        if not nonempty_str(api_group) or not nonempty_str(role_kind) or not nonempty_str(role_name):
            sys.exit(14)
        is_cluster_admin = (
            api_group == "rbac.authorization.k8s.io"
            and role_kind == "ClusterRole"
            and role_name == "cluster-admin"
        )
        if "subjects" not in item:
            continue
        subj_list = item["subjects"]
        if not isinstance(subj_list, list):
            sys.exit(14)
        for subj in subj_list:
            if not isinstance(subj, dict):
                sys.exit(14)
            kind = subj.get("kind")
            name = subj.get("name")
            if kind not in ("User", "Group", "ServiceAccount") or not nonempty_str(name):
                sys.exit(14)
            if "apiGroup" in subj and not isinstance(subj.get("apiGroup"), str):
                sys.exit(14)
            if kind == "ServiceAccount":
                namespace = subj.get("namespace")
                if not nonempty_str(namespace):
                    sys.exit(14)
                if is_cluster_admin and namespace == ns and name == sa:
                    matched = True
            elif "namespace" in subj and not isinstance(subj.get("namespace"), str):
                sys.exit(14)
    sys.exit(0 if matched else 10)
except SystemExit:
    raise
except Exception:
    sys.exit(13)
' "${ns}" "${sa}"
}

# Process-bound list. Non-zero (including timeout 124) means the API response is unusable.
_oc_get_clusterrolebindings_json() {
  local req="${OC_REQUEST_TIMEOUT:-45s}"
  timeout "${req}" oc --request-timeout="${req}" get clusterrolebinding -o json
}

_confirm_rbac_removed() {
  local ns="${SCANNER_NAMESPACE:-${TLS_SCANNER_POD_NAMESPACE:-tls-scanner}}"
  local sa="${SCANNER_SERVICE_ACCOUNT_NAME:-tls-scanner}"
  local req="${OC_REQUEST_TIMEOUT:-45s}"
  echo "Confirming cluster-admin is absent for system:serviceaccount:${ns}:${sa}"
  # Best-effort removal in case cleanup ran while the API was stalled.
  timeout "${req}" oc --request-timeout="${req}" adm policy remove-cluster-role-from-user cluster-admin -z "${sa}" -n "${ns}" || true
  local json st=0
  if ! json=$(_oc_get_clusterrolebindings_json 2>/dev/null); then
    echo "ERROR: could not list ClusterRoleBindings (request bound by ${req})" >&2
    return 1
  fi
  printf '%s' "${json}" | _classify_crb_json "${ns}" "${sa}" || st=$?
  if [[ "${st}" -eq 10 ]]; then
    echo "Confirmed: no cluster-admin binding for system:serviceaccount:${ns}:${sa}"
    return 0
  fi
  if [[ "${st}" -eq 0 ]]; then
    echo "ERROR: cluster-admin binding still present for ${sa} in ${ns}" >&2
    return 1
  fi
  echo "ERROR: cannot confirm cluster-admin removal (classifier status ${st})" >&2
  return 1
}

_rbac_selftest() {
  local failed=0
  _expect() {
    local want="$1" json="$2" st=0
    printf '%s' "${json}" | _classify_crb_json "ns" "sa" || st=$?
    if [[ "${st}" -ne "${want}" ]]; then
      echo "FAIL want ${want} got ${st}: ${json}" >&2
      failed=1
    fi
  }
  local cr='{"apiGroup":"rbac.authorization.k8s.io","kind":"ClusterRole","name":"cluster-admin"}'
  _expect 12 '{}'
  _expect 12 '{"items":null}'
  _expect 10 '{"items":[]}'
  _expect 11 'not-json'
  _expect 14 '{"items":[null]}'
  _expect 14 "{\"items\":[{\"roleRef\":{}}]}"
  _expect 14 "{\"items\":[{\"roleRef\":{\"apiGroup\":\"rbac.authorization.k8s.io\",\"kind\":42,\"name\":\"cluster-admin\"}}]}"
  _expect 10 "{\"items\":[{\"roleRef\":${cr}}]}"
  _expect 10 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[]}]}"
  _expect 14 "{\"items\":[{\"roleRef\":${cr},\"subjects\":null}]}"
  _expect 14 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[{}]}]}"
  _expect 14 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[{\"kind\":42,\"name\":\"sa\",\"namespace\":\"ns\"}]}]}"
  _expect 14 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[{\"kind\":\"nope\",\"name\":\"sa\",\"namespace\":\"ns\"}]}]}"
  _expect 14 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[{\"kind\":\"ServiceAccount\",\"name\":\"sa\"}]}]}"
  _expect 0 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[{\"kind\":\"ServiceAccount\",\"name\":\"sa\",\"namespace\":\"ns\"}]}]}"
  _expect 10 "{\"items\":[{\"roleRef\":${cr},\"subjects\":[{\"kind\":\"ServiceAccount\",\"name\":\"other\",\"namespace\":\"ns\"}]}]}"
  _expect 14 "{\"items\":[{\"roleRef\":{\"apiGroup\":\"rbac.authorization.k8s.io\",\"kind\":\"ClusterRole\",\"name\":\"view\"},\"subjects\":[{\"kind\":42,\"name\":\"x\"}]}]}"
  local stall_dir start end stall_st=0
  stall_dir=$(mktemp -d)
  cat > "${stall_dir}/oc" <<'EOF'
#!/bin/sh
sleep 30
EOF
  chmod +x "${stall_dir}/oc"
  start=$(date +%s)
  PATH="${stall_dir}:${PATH}" OC_REQUEST_TIMEOUT=2s _oc_get_clusterrolebindings_json >/dev/null 2>&1 || stall_st=$?
  end=$(date +%s)
  rm -rf "${stall_dir}"
  if [[ "${stall_st}" -eq 0 || $((end - start)) -gt 5 ]]; then
    echo "FAIL hung oc was not bounded (status ${stall_st}, elapsed $((end - start))s)" >&2
    failed=1
  fi
  if [[ "${failed}" -ne 0 ]]; then
    echo "RBAC self-test failed" >&2
    return 1
  fi
  echo "RBAC self-test passed"
}

run_tls_scan() {

  # Bound individual oc API calls (stalled server connections) separately from scan wall-clock deadline.
  # Note: --request-timeout bounds single REST requests, not long streams — use `timeout` for logs/cp/wait.
  local OC_REQUEST_TIMEOUT="${OC_REQUEST_TIMEOUT:-45s}"
  local OC_CP_REQUEST_TIMEOUT="${OC_CP_REQUEST_TIMEOUT:-120s}"
  local OC_LOGS_STREAM_TIMEOUT="${OC_LOGS_STREAM_TIMEOUT:-600s}"
  local OC_LOGS_WAIT_TIMEOUT="${OC_LOGS_WAIT_TIMEOUT:-300s}"
  local OC_CP_PROCESS_TIMEOUT="${OC_CP_PROCESS_TIMEOUT:-600s}"
  local OC_EXEC_PROCESS_TIMEOUT="${OC_EXEC_PROCESS_TIMEOUT:-60s}"
  local -a OCR=(oc --request-timeout="${OC_REQUEST_TIMEOUT}")
  local -a OCR_CP=(oc --request-timeout="${OC_CP_REQUEST_TIMEOUT}")
  if ! command -v timeout >/dev/null 2>&1; then
      echo "ERROR: \`timeout\` not found in PATH — need coreutils for bounded oc logs/cp/wait" >&2
      exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
      echo "ERROR: python3 required for cluster-admin binding verification" >&2
      exit 1
  fi

  # `wait` is a shell builtin — cannot use `timeout wait` (exits 127). Use wall-clock drain instead.
  _duration_to_sec() {
      local v="${1:-300s}"
      if [[ "${v}" =~ ^[0-9]+$ ]]; then
          echo "${v}"
      elif [[ "${v}" =~ ^([0-9]+)s$ ]]; then
          echo "${BASH_REMATCH[1]}"
      else
          echo "300"
      fi
  }

  wait_for_logs_follower() {
      local pid="${1:?}"
      local max_sec
      max_sec=$(_duration_to_sec "${OC_LOGS_WAIT_TIMEOUT}")
      local start
      start=$(date +%s)
      while kill -0 "${pid}" 2>/dev/null; do
          local now
          now=$(date +%s)
          if (( now - start >= max_sec )); then
              echo "Log follower still running after ${max_sec}s — sending SIGKILL to pid ${pid}" >&2
              kill -9 "${pid}" 2>/dev/null || true
              wait "${pid}" 2>/dev/null || true
              return 124
          fi
          sleep 1
      done
      wait "${pid}" 2>/dev/null || true
      return 0
  }

  local scanner_pod_mode="${SCANNER_POD_MODE:-false}"
  case "${scanner_pod_mode}" in
    true|false) ;;
    *) echo "Invalid SCANNER_POD_MODE: expected true or false" >&2; exit 1 ;;
  esac
  if [[ "${scanner_pod_mode}" == "true" && ( -n "${SCANNER_NAMESPACE:-}" || -n "${TLS_SCANNER_CLUSTER_LABEL:-}" ) ]]; then
      echo "SCANNER_POD_MODE requires the scanner-owned namespace and no cluster label" >&2
      exit 1
  fi

  # TLS Scanner - scans TLS configurations of all pods in the cluster
  local NAMESPACE="${SCANNER_NAMESPACE:-tls-scanner}"
  local OWNS_NAMESPACE=true
  # When SCANNER_NAMESPACE is explicitly set, deploy into an existing namespace
  # (do not create/delete it). Check the variable itself, not the resolved value,
  # so that SCANNER_NAMESPACE=tls-scanner is also treated as externally owned.
  if [[ -n "${SCANNER_NAMESPACE:-}" ]]; then
      OWNS_NAMESPACE=false
  fi
  local SCANNER_SA_NAME="${SCANNER_SERVICE_ACCOUNT_NAME:-tls-scanner}"
  local SCANNER_IMAGE="${PULL_SPEC_TLS_SCANNER_TOOL}"
  local ARTIFACT_DIR="${ARTIFACT_DIR:-/tmp/artifacts}"

  if [[ -n "${TLS_SCANNER_CLUSTER_LABEL:-}" ]]; then
    if [[ "${PQC_CHECK:-false}" == "true" ]]; then
      SCANNER_ARTIFACT_DIR="${ARTIFACT_DIR}/tls-scanner/pqc-${TLS_SCANNER_CLUSTER_LABEL}"
    else
      SCANNER_ARTIFACT_DIR="${ARTIFACT_DIR}/tls-scanner/${TLS_SCANNER_CLUSTER_LABEL}"
    fi
    case "${TLS_SCANNER_CLUSTER_LABEL}" in
      management)
        export KUBECONFIG="${SHARED_DIR}/kubeconfig"
        if [[ -z "${SCAN_NAMESPACE:-}" && -f "${SHARED_DIR}/cluster-name" ]]; then
          CLUSTER_NAME="$(<"${SHARED_DIR}/cluster-name")"
          SCAN_NAMESPACE="clusters-${CLUSTER_NAME}"
        fi
        ;;
      guest)
        export KUBECONFIG="${SHARED_DIR}/nested_kubeconfig"
        ;;
      *)
        echo "Unknown TLS_SCANNER_CLUSTER_LABEL: ${TLS_SCANNER_CLUSTER_LABEL}"
        exit 1
        ;;
    esac
  else
    SCANNER_ARTIFACT_DIR="${ARTIFACT_DIR}/tls-scanner"
  fi

  echo "TLS scanner target: ${TLS_SCANNER_CLUSTER_LABEL:-default} cluster"
  echo "KUBECONFIG: ${KUBECONFIG:-<unset>}"

  # Determine scanner arguments based on whether a specific namespace is requested
  if [[ -n "${SCAN_NAMESPACE:-}" ]]; then
      local ns_regex='^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
      local normalized=""
      local part
      local -a ns_parts=()
      if [[ "${SCAN_NAMESPACE}" == *$'\n'* ]]; then
          echo "Invalid SCAN_NAMESPACE: newline is not allowed" >&2
          exit 1
      fi
      IFS=',' read -r -a ns_parts <<< "${SCAN_NAMESPACE}"
      for part in "${ns_parts[@]}"; do
          part="${part#"${part%%[![:space:]]*}"}"
          part="${part%"${part##*[![:space:]]}"}"
          if [[ -z "${part}" ]]; then
              echo "Invalid SCAN_NAMESPACE: empty entry in comma-separated list" >&2
              exit 1
          fi
          if [[ "${#part}" -gt 63 ]]; then
              echo "Invalid SCAN_NAMESPACE: '${part}' exceeds 63 character DNS-1123 limit" >&2
              exit 1
          fi
          if [[ "${part}" =~ [[:space:]] ]]; then
              echo "Invalid SCAN_NAMESPACE: whitespace inside namespace name '${part}'" >&2
              exit 1
          fi
          if [[ ! "${part}" =~ ${ns_regex} ]]; then
              echo "Invalid SCAN_NAMESPACE: '${part}' is not a valid Kubernetes namespace name" >&2
              exit 1
          fi
          if [[ -n "${normalized}" ]]; then
              normalized+=","
          fi
          normalized+="${part}"
      done
      if [[ -z "${normalized}" ]]; then
          echo "Invalid SCAN_NAMESPACE: no namespaces after validation" >&2
          exit 1
      fi
      SCAN_NAMESPACE="${normalized}"
      echo "Namespace filter (validated): ${SCAN_NAMESPACE}"
      SCANNER_ARGS="--all-pods --namespace-filter ${SCAN_NAMESPACE}"
  else
      SCANNER_ARGS="--all-pods"
  fi

  if [[ -n "${COMPONENT_FILTER:-}" ]]; then
      local component_name='[a-zA-Z0-9]([a-zA-Z0-9_.-]*[a-zA-Z0-9])?'
      local component_list="^[[:blank:]]*${component_name}[[:blank:]]*(,[[:blank:]]*${component_name}[[:blank:]]*)*$"
      if [[ ! "${COMPONENT_FILTER}" =~ ${component_list} ]]; then
          echo "Invalid COMPONENT_FILTER: expected comma-separated component names" >&2
          exit 1
      fi
      local component_filter_arg
      printf -v component_filter_arg '%q' "--component-filter=${COMPONENT_FILTER}"
      SCANNER_ARGS="${SCANNER_ARGS} ${component_filter_arg}"
      echo "Component filter: ${COMPONENT_FILTER}"
  fi

  # Enable post-quantum cryptography checks when requested by the step ref.
  if [[ "${PQC_CHECK:-false}" == "true" ]]; then
      SCANNER_ARGS="${SCANNER_ARGS} --pqc-check"
      echo "PQC readiness mode enabled: checks TLS 1.3 support and mlkem or mlkem25519 support per target."
  fi

  # Assert TLS named groups/curves independently of PQC readiness.
  if [[ -n "${EXPECTED_GROUPS:-}" ]]; then
      local groups_mode="${EXPECTED_GROUPS_MODE:-contains}"
      case "${groups_mode}" in
        contains|exact) ;;
        *)
          echo "Invalid EXPECTED_GROUPS_MODE='${groups_mode}' (expected contains or exact)"
          exit 1
          ;;
      esac
      # SCANNER_ARGS is expanded unquoted into the scanner pod script, so strip
      # whitespace from the group list (e.g. "X25519, x25519" -> "X25519,x25519").
      local expected_groups="${EXPECTED_GROUPS//[[:space:]]/}"
      if [[ -z "${expected_groups}" ]]; then
          echo "EXPECTED_GROUPS must list at least one group after removing whitespace"
          exit 1
      fi
      SCANNER_ARGS="${SCANNER_ARGS} --expected-groups=${expected_groups} --expected-groups-mode=${groups_mode}"
      echo "Expected groups check enabled: groups=${expected_groups} mode=${groups_mode}"
  fi

  if [[ -n "${SCAN_LIMIT_IPS:-}" && "${SCAN_LIMIT_IPS}" != "0" ]]; then
      SCANNER_ARGS="${SCANNER_ARGS} --limit-ips ${SCAN_LIMIT_IPS}"
      echo "Limiting scan to ${SCAN_LIMIT_IPS} IPs (smoke testing)."
  fi

  if [[ -n "${TLS_PROFILE_TYPE:-}" ]]; then
      SCANNER_ARGS="${SCANNER_ARGS} --tls-profile-type ${TLS_PROFILE_TYPE}"
      echo "Using expected TLS profile type for compliance checks: ${TLS_PROFILE_TYPE}"
  fi

  # SCANNER_CPU / SCANNER_MEMORY are deprecated but still honoured as fallback
  # defaults for both request and limit when the split vars aren't explicitly
  # set, so that a lower CPU request (e.g. 500m) can get the Pod scheduled
  # while still allowing it to burst up to the higher limit.
  local scanner_cpu_request="${SCANNER_CPU_REQUEST:-${SCANNER_CPU:-4}}"
  local scanner_cpu_limit="${SCANNER_CPU_LIMIT:-${SCANNER_CPU:-4}}"
  local scanner_mem_request="${SCANNER_MEM_REQUEST:-${SCANNER_MEMORY:-4Gi}}"
  local scanner_mem_limit="${SCANNER_MEM_LIMIT:-${SCANNER_MEMORY:-4Gi}}"
  if [[ "${TLS_SCANNER_CLUSTER_LABEL:-}" == "guest" ]]; then
    scanner_cpu_request="${SCANNER_CPU_GUEST_REQUEST:-${SCANNER_CPU_GUEST}}"
    scanner_cpu_limit="${SCANNER_CPU_GUEST_LIMIT:-${SCANNER_CPU_GUEST}}"
    scanner_mem_request="${SCANNER_MEM_GUEST_REQUEST:-${SCANNER_MEMORY_GUEST}}"
    scanner_mem_limit="${SCANNER_MEM_GUEST_LIMIT:-${SCANNER_MEMORY_GUEST}}"
  else
    # When only a limit is set, clamp request down to limit if legacy SCANNER_CPU exceeds it.
    # Preserve a lower request (e.g. 500m) when limit is higher (schedule + burst).
    if [[ -n "${SCANNER_CPU_LIMIT:-}" && -z "${SCANNER_CPU_REQUEST:-}" ]]; then
      local req_m lim_m
      req_m=$(echo "${scanner_cpu_request}" | awk '/m$/ { sub(/m$/,""); print $0+0; next } { print ($0+0)*1000 }')
      lim_m=$(echo "${scanner_cpu_limit}" | awk '/m$/ { sub(/m$/,""); print $0+0; next } { print ($0+0)*1000 }')
      if awk "BEGIN { exit !(${req_m} > ${lim_m}) }"; then
        scanner_cpu_request="${scanner_cpu_limit}"
      fi
    fi
    if [[ -n "${SCANNER_MEM_LIMIT:-}" && -z "${SCANNER_MEM_REQUEST:-}" ]]; then
      scanner_mem_request="${scanner_mem_limit}"
    fi
  fi
  echo "Scanner pod resources: cpu=${scanner_cpu_request}/${scanner_cpu_limit} memory=${scanner_mem_request}/${scanner_mem_limit}"

  mkdir -p "${SCANNER_ARTIFACT_DIR}"

  echo "=== TLS Scanner ==="
  echo "Image: ${SCANNER_IMAGE}"

  # For management cluster scans, deploy the scanner pod into the HCP namespace
  # so it satisfies the HCP NetworkPolicy intra-namespace allow rules.
  # The HCP namespace already exists and must not be deleted at cleanup.
  if [[ "${TLS_SCANNER_CLUSTER_LABEL:-}" == "management" && -n "${SCAN_NAMESPACE:-}" ]]; then
      NAMESPACE="${SCAN_NAMESPACE}"
      OWNS_NAMESPACE=false
  fi

  if [[ "${OWNS_NAMESPACE}" == "true" ]]; then
      # Shared clusters can retain tls-scanner resources from prior jobs.
      # Pods are immutable; delete any leftover namespace before recreating.
      echo "Removing any previous tls-scanner resources..."
      "${OCR[@]}" delete namespace "${NAMESPACE}" --ignore-not-found --wait=true --timeout=120s || true
      "${OCR[@]}" create namespace "${NAMESPACE}"
  else
      # Just remove any leftover scanner pod from a prior run; do not touch the namespace.
      "${OCR[@]}" delete pod/tls-scanner -n "${NAMESPACE}" --ignore-not-found --wait=true --timeout=60s || true
  fi

  # Dedicated SA for cluster-wide API access; never use the namespace default SA.
  "${OCR[@]}" create serviceaccount "${SCANNER_SA_NAME}" -n "${NAMESPACE}" --dry-run=client -o yaml | "${OCR[@]}" apply -f -

  # Snapshot locals into global variables so the EXIT trap can reference them
  # after run_tls_scan returns and locals go out of scope.
  _CLEANUP_NAMESPACE="${NAMESPACE}"
  _CLEANUP_OWNS_NAMESPACE="${OWNS_NAMESPACE}"
  _CLEANUP_SCANNER_SA_NAME="${SCANNER_SA_NAME}"

  cluster_admin_binding_present() {
      local ns="$1"
      local sa="$2"
      local json st=0
      # timeout + --request-timeout: a stalled apiserver cannot hold this call open.
      if ! json=$(_oc_get_clusterrolebindings_json 2>/dev/null); then
          return 2
      fi
      printf '%s' "${json}" | _classify_crb_json "${ns}" "${sa}" || st=$?
      return "${st}"
  }

  cleanup_scanner_rbac() {
      local ns="${_CLEANUP_NAMESPACE}"
      local sa="${_CLEANUP_SCANNER_SA_NAME}"
      local max_attempts="${RBAC_CLEANUP_VERIFY_ATTEMPTS:-15}"
      local sleep_sec="${RBAC_CLEANUP_VERIFY_SLEEP_SEC:-4}"
      local wall_sec="${RBAC_CLEANUP_WALL_TIMEOUT:-180}"
      local deadline_epoch=$(( $(date +%s) + wall_sec ))
      local attempt
      echo "Removing tls-scanner RBAC for SA ${sa} in ${ns}..."
      if ! timeout "${OC_REQUEST_TIMEOUT}" "${OCR[@]}" adm policy remove-cluster-role-from-user cluster-admin -z "${sa}" -n "${ns}"; then
          echo "ERROR: cluster-admin removal for ${sa} in ${ns} failed (bound by ${OC_REQUEST_TIMEOUT})" >&2
          return 1
      fi
      for (( attempt=1; attempt<=max_attempts; attempt++ )); do
          if (( $(date +%s) >= deadline_epoch )); then
              echo "ERROR: RBAC verification exceeded wall-clock ${wall_sec}s" >&2
              return 1
          fi
          cluster_admin_binding_present "${ns}" "${sa}"
          local st=$?
          if [[ "${st}" -eq 10 ]]; then
              echo "Verified: no cluster-admin ClusterRoleBinding for system:serviceaccount:${ns}:${sa}"
              break
          fi
          if [[ "${st}" -eq 2 ]]; then
              echo "Warning: could not list clusterrolebindings (attempt ${attempt}/${max_attempts}), retrying..." >&2
          elif [[ "${st}" -eq 11 ]]; then
              echo "ERROR: JSON parse error from clusterrolebinding list — cannot verify RBAC" >&2
              return 1
          elif [[ "${st}" -eq 12 ]]; then
              echo "ERROR: unexpected ClusterRoleBinding list shape (expected .items list) — cannot verify RBAC" >&2
              return 1
          elif [[ "${st}" -eq 14 ]]; then
              echo "ERROR: malformed ClusterRoleBinding entry in list — cannot verify RBAC" >&2
              return 1
          elif [[ "${st}" -eq 13 ]]; then
              echo "ERROR: internal error while parsing ClusterRoleBinding list — cannot verify RBAC" >&2
              return 1
          else
              echo "Waiting for cluster-admin binding removal (attempt ${attempt}/${max_attempts})..."
          fi
          sleep "${sleep_sec}"
      done
      cluster_admin_binding_present "${ns}" "${sa}"
      local final_st=$?
      if [[ "${final_st}" -eq 0 ]]; then
          echo "ERROR: cluster-admin binding still present for ${sa} in ${ns} after cleanup" >&2
          return 1
      fi
      if [[ "${final_st}" -eq 2 ]]; then
          echo "ERROR: could not confirm cluster-admin removal — API unavailable after ${max_attempts} attempts" >&2
          return 1
      fi
      if [[ "${final_st}" -eq 10 ]]; then
          :
      elif [[ "${final_st}" -eq 11 ]]; then
          echo "ERROR: JSON parse error from clusterrolebinding list — cannot verify RBAC" >&2
          return 1
      elif [[ "${final_st}" -eq 12 ]]; then
          echo "ERROR: unexpected ClusterRoleBinding list shape — cannot verify RBAC" >&2
          return 1
      elif [[ "${final_st}" -eq 14 ]]; then
          echo "ERROR: malformed ClusterRoleBinding entry in list — cannot verify RBAC" >&2
          return 1
      elif [[ "${final_st}" -eq 13 ]]; then
          echo "ERROR: internal error while parsing ClusterRoleBinding list — cannot verify RBAC" >&2
          return 1
      else
          echo "ERROR: unexpected exit code from cluster-admin verification (${final_st})" >&2
          return 1
      fi
      if ! timeout "${OC_REQUEST_TIMEOUT}" "${OCR[@]}" adm policy remove-scc-from-user privileged -z "${sa}" -n "${ns}" 2>/dev/null; then
          echo "Warning: privileged SCC removal for ${sa} in ${ns} failed or timed out (request-timeout=${OC_REQUEST_TIMEOUT})" >&2
      fi
      return 0
  }

  cleanup() {
      echo "Cleaning up..."
      # Best-effort on EXIT trap (abnormal path); success path verifies RBAC via cleanup_scanner_rbac || exit 1
      cleanup_scanner_rbac || echo "Warning: RBAC cleanup incomplete during EXIT trap" >&2
      if [[ "${_CLEANUP_OWNS_NAMESPACE}" == "true" ]]; then
          "${OCR[@]}" delete namespace "${_CLEANUP_NAMESPACE}" --ignore-not-found --wait=false || true
      else
          "${OCR[@]}" delete pod/tls-scanner -n "${_CLEANUP_NAMESPACE}" --ignore-not-found --wait=false || true
      fi
  }
  trap cleanup EXIT

  # hostNetwork/hostPID are required for host-mode scanning but defeat NetworkPolicy
  # for pod-mode management cluster scans (host-networked pods source from the node
  # IP, which does not match any pod/namespace selector in HCP ingress rules).
  # Pod-mode scanning uses the kube API for discovery and exec, so neither is needed.
  local pod_annotations=""
  local run_as_user_yaml="      runAsUser: 65532"
  if [[ "${scanner_pod_mode}" == "true" ]]; then
      pod_annotations="  annotations:
    openshift.io/required-scc: restricted-v2"
      run_as_user_yaml=""
  fi
  if [[ "${OWNS_NAMESPACE}" == "false" || "${scanner_pod_mode}" == "true" ]]; then
      HOST_NETWORK="false"
      HOST_PID="false"
      # PodSecurity restricted-compliant securityContext for HCP namespace scans.
      # The scanner binary only needs kube API access in pod-mode, so root is not required.
      SECURITY_CONTEXT_YAML="      allowPrivilegeEscalation: false
      runAsNonRoot: true
${run_as_user_yaml}
      capabilities:
        drop:
        - ALL
      seccompProfile:
        type: RuntimeDefault"
  else
      HOST_NETWORK="true"
      HOST_PID="true"
      SECURITY_CONTEXT_YAML="      privileged: true
      runAsUser: 0"
  fi

  # Grant cluster-admin to the dedicated scanner SA (not the default namespace SA).
  "${OCR[@]}" adm policy add-cluster-role-to-user cluster-admin -z "${SCANNER_SA_NAME}" -n "${NAMESPACE}"

  # Grant privileged SCC to the service account (required for hostNetwork/hostPID/privileged
  # container in host-mode scans).
  if [[ "${OWNS_NAMESPACE}" == "true" && "${scanner_pod_mode}" == "false" ]]; then
      "${OCR[@]}" adm policy add-scc-to-user privileged -z "${SCANNER_SA_NAME}" -n "${NAMESPACE}"
  fi

  # Wait for RBAC/SCC changes to propagate before creating the pod
  # This ensures the SCC admission controller sees the new binding
  echo "Waiting for RBAC/SCC changes to propagate..."
  sleep 10

  # Optional pod labels (YAML lines under metadata.labels), e.g. MaaS maas-api NetworkPolicy probe.
  local extra_pod_labels=""
  if [[ "${SCANNER_MAAS_API_E2E_PROBE:-}" == "true" ]]; then
      extra_pod_labels="${extra_pod_labels}
    maas-api-e2e-probe: \"true\""
  fi
  if [[ -n "${SCANNER_EXTRA_POD_LABELS:-}" ]]; then
      extra_pod_labels="${extra_pod_labels}
${SCANNER_EXTRA_POD_LABELS}"
  fi

  # Create the scanner pod
  cat <<EOF | "${OCR[@]}" create -f -
apiVersion: v1
kind: Pod
metadata:
  name: tls-scanner
  namespace: ${NAMESPACE}
${pod_annotations}
  labels:
    app: tls-scanner
    # Required when running in an HCP namespace (OWNS_NAMESPACE=false): HyperShift's
    # management-kas NetworkPolicy isolates pods without this label from the management
    # cluster kube API (172.30.x.x service CIDR) via OVN ACLs that sit below standard
    # NetworkPolicy. The label exempts the scanner so it can list pods via the
    # management cluster API, which is the first thing tls-scanner does on startup.
    hypershift.openshift.io/need-management-kas-access: "true"${extra_pod_labels}
spec:
  serviceAccountName: ${SCANNER_SA_NAME}
  restartPolicy: Never
  hostNetwork: ${HOST_NETWORK}
  hostPID: ${HOST_PID}
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
      # Keep pod alive for artifact collection before exiting with the scanner's code.
      sleep 120
      exit \${SCAN_EXIT_CODE}
    resources:
      requests:
        cpu: "${scanner_cpu_request}"
        memory: ${scanner_mem_request}
      limits:
        cpu: "${scanner_cpu_limit}"
        memory: ${scanner_mem_limit}
    securityContext:
${SECURITY_CONTEXT_YAML}
    volumeMounts:
    - name: results
      mountPath: /results
  volumes:
  - name: results
    emptyDir: {}
EOF

  echo "Waiting for scanner pod to start..."
  "${OCR[@]}" wait --for=condition=Ready pod/tls-scanner -n "${NAMESPACE}" --timeout=5m || {
      echo "Pod failed to start:"
      "${OCR[@]}" describe pod/tls-scanner -n "${NAMESPACE}"
      "${OCR[@]}" get events -n "${NAMESPACE}"
      exit 1
  }

  echo "Streaming scanner logs (live, process bound ${OC_LOGS_STREAM_TIMEOUT})..."
  # shellcheck disable=SC2086
  timeout "${OC_LOGS_STREAM_TIMEOUT}" "${OCR[@]}" logs -f pod/tls-scanner -n "${NAMESPACE}" &
  LOGS_PID=$!

  # Wall-clock deadline for scan.done (API calls are additionally bounded by --request-timeout).
  local scan_done_found=false
  local completion_timed_out=false
  local completion_poll_interval="${SCAN_COMPLETION_POLL_INTERVAL:-15}"
  local completion_wall_deadline_sec="${SCAN_COMPLETION_WAIT_TIMEOUT:-900}"
  local completion_deadline_epoch
  completion_deadline_epoch=$(( $(date +%s) + completion_wall_deadline_sec ))
  echo "Waiting for scan to finish (pod stays alive 120s after scan for artifact collection; wall-clock deadline ${completion_wall_deadline_sec}s)..."
  while true; do
      local now_sec
      now_sec=$(date +%s)
      if (( now_sec >= completion_deadline_epoch )); then
          completion_timed_out=true
          break
      fi
      phase=$("${OCR[@]}" get pod/tls-scanner -n "${NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
      echo "Poll: phase=${phase}"
      # Scanner completion check first — must copy artifacts while pod is still running.
      # shellcheck disable=SC2086
      if timeout "${OC_EXEC_PROCESS_TIMEOUT}" "${OCR[@]}" exec pod/tls-scanner -n "${NAMESPACE}" -- test -f /results/scan.done 2>/dev/null; then
          echo "/results/scan.done found — proceeding to copy artifacts"
          scan_done_found=true
          break
      fi
      # Fallback: pod already exited (sleep window expired or crash).
      if [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]]; then
          echo "Warning: pod ${phase} before artifact collection — oc cp will likely fail"
          break
      fi
      sleep "${completion_poll_interval}"
  done

  if [[ "${completion_timed_out}" == "true" ]] && [[ "${scan_done_found}" != "true" ]]; then
      echo "ERROR: wall-clock deadline (${completion_wall_deadline_sec}s) expired waiting for /results/scan.done (phase=${phase})" >&2
      kill "${LOGS_PID}" 2>/dev/null || true
      wait_for_logs_follower "${LOGS_PID}" || true
      echo "Copying artifacts (partial)..."
      # shellcheck disable=SC2086
      timeout "${OC_CP_PROCESS_TIMEOUT}" "${OCR_CP[@]}" cp "${NAMESPACE}/tls-scanner:/results/." "${SCANNER_ARTIFACT_DIR}/" || echo "Warning: Failed to copy some artifacts"
      "${OCR[@]}" describe pod/tls-scanner -n "${NAMESPACE}" >&2 || true
      exit 1
  fi

  echo "Copying artifacts..."
  # shellcheck disable=SC2086
  timeout "${OC_CP_PROCESS_TIMEOUT}" "${OCR_CP[@]}" cp "${NAMESPACE}/tls-scanner:/results/." "${SCANNER_ARTIFACT_DIR}/" || echo "Warning: Failed to copy some artifacts"

  if [[ -f "${SCANNER_ARTIFACT_DIR}/junit_tls_scan.xml" ]]; then
      if [[ "${PQC_CHECK:-false}" == "true" && -n "${TLS_SCANNER_CLUSTER_LABEL:-}" ]]; then
        junit_artifact="${ARTIFACT_DIR}/junit_pqc_scan_${TLS_SCANNER_CLUSTER_LABEL}.xml"
      elif [[ -n "${TLS_SCANNER_CLUSTER_LABEL:-}" ]]; then
        junit_artifact="${ARTIFACT_DIR}/junit_tls_scan_${TLS_SCANNER_CLUSTER_LABEL}.xml"
      else
        junit_artifact="${ARTIFACT_DIR}/junit_tls_scan.xml"
      fi
      cp "${SCANNER_ARTIFACT_DIR}/junit_tls_scan.xml" "${junit_artifact}"
      echo "JUnit results copied to ${junit_artifact} for Spyglass"
  fi

  wait_for_logs_follower "${LOGS_PID}" || true

  # Check final pod status. The scanner exits non-zero when TLS findings are
  # detected — this is expected and not an infrastructure failure. Propagate
  # the scanner's exit code so CI marks the job accordingly, but do not treat
  # it as a timeout or describe the pod as failed when artifacts were collected.
  local pod_phase
  pod_phase="$("${OCR[@]}" get pod/tls-scanner -n "${NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")"
  local pod_exit_code
  pod_exit_code="$("${OCR[@]}" get pod/tls-scanner -n "${NAMESPACE}" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || echo "")"

  if [[ "${pod_phase}" == "Failed" ]]; then
      echo "Scanner pod exited with phase=${pod_phase} code=${pod_exit_code}"
      if [[ -f "${SCANNER_ARTIFACT_DIR}/junit_tls_scan.xml" ]]; then
          echo "Artifacts were collected — scanner found TLS compliance issues."
      else
          echo "No artifacts found — scanner may have crashed."
          "${OCR[@]}" describe pod/tls-scanner -n "${NAMESPACE}"
      fi
      exit "${pod_exit_code:-1}"
  elif [[ "${pod_phase}" != "Succeeded" ]]; then
      # Wall-clock bound only (each oc get may take up to OC_REQUEST_TIMEOUT).
      local pod_terminal_poll_sec="${POD_TERMINAL_POLL_TIMEOUT:-600}"
      local poll_interval="${POD_TERMINAL_POLL_INTERVAL:-10}"
      local pod_terminal_deadline_epoch
      pod_terminal_deadline_epoch=$(( $(date +%s) + pod_terminal_poll_sec ))
      echo "Scanner pod in unexpected phase: ${pod_phase} — polling until terminal (wall-clock ${pod_terminal_poll_sec}s)"
      while true; do
          local poll_now_sec
          poll_now_sec=$(date +%s)
          if (( poll_now_sec >= pod_terminal_deadline_epoch )); then
              break
          fi
          pod_phase="$("${OCR[@]}" get pod/tls-scanner -n "${NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")"
          if [[ "${pod_phase}" == "Succeeded" || "${pod_phase}" == "Failed" ]]; then
              break
          fi
          sleep "${poll_interval}"
      done
      if [[ "${pod_phase}" == "Failed" ]]; then
          pod_exit_code="$("${OCR[@]}" get pod/tls-scanner -n "${NAMESPACE}" -o jsonpath='{.status.containerStatuses[0].state.terminated.exitCode}' 2>/dev/null || echo "")"
          echo "Scanner pod exited with phase=${pod_phase} code=${pod_exit_code}"
          if [[ -f "${SCANNER_ARTIFACT_DIR}/junit_tls_scan.xml" ]]; then
              echo "Artifacts were collected — scanner found TLS compliance issues."
          else
              echo "No artifacts found — scanner may have crashed."
              "${OCR[@]}" describe pod/tls-scanner -n "${NAMESPACE}"
          fi
          exit "${pod_exit_code:-1}"
      elif [[ "${pod_phase}" != "Succeeded" ]]; then
          echo "Scanner did not complete successfully - timeout exceeded (phase: ${pod_phase})"
          "${OCR[@]}" describe pod/tls-scanner -n "${NAMESPACE}"
          exit 1
      fi
  fi

  echo "=== TLS Scanner Complete ==="
  echo "Artifacts saved to: ${SCANNER_ARTIFACT_DIR}"
  ls -la "${SCANNER_ARTIFACT_DIR}" || true

  # Drop elevated bindings before returning (namespace may outlive this job).
  cleanup_scanner_rbac || exit 1

  # Unregister the trap so it doesn't fire after the function returns (local
  # variables NAMESPACE/OWNS_NAMESPACE would be unbound at that point).
  if [[ "${scanner_pod_mode}" == "true" ]]; then
      cleanup
  fi
  trap - EXIT
}

if [[ "${1:-}" == "--rbac-selftest" ]]; then
  _rbac_selftest
  exit $?
fi
if [[ "${1:-}" == "--confirm-rbac-removed" ]]; then
  _confirm_rbac_removed
  exit $?
fi

if [[ "${TLS_SCANNER_RUN_HYPERSHIFT:-false}" == "true" ]]; then
  OVERALL_EXIT_CODE=0

  # First pass: TLS compliance only. Explicitly override PQC_CHECK in subshells.
  for label in management guest; do
    echo "=== TLS scanner: ${label} cluster ==="
    (
      export TLS_SCANNER_CLUSTER_LABEL="${label}"
      export PQC_CHECK="false"
      run_tls_scan
    ) || OVERALL_EXIT_CODE=1
    echo "=== TLS scanner: ${label} cluster complete ==="
  done

  # Second pass: PQC readiness check (only if PQC_CHECK was originally enabled).
  if [[ "${PQC_CHECK:-false}" == "true" ]]; then
    echo "=== TLS scanner: PQC readiness pass ==="
    for label in management guest; do
      echo "=== PQC scanner: ${label} cluster ==="
      (
        export TLS_SCANNER_CLUSTER_LABEL="${label}"
        export PQC_CHECK="true"
        run_tls_scan
      ) || OVERALL_EXIT_CODE=1
      echo "=== PQC scanner: ${label} cluster complete ==="
    done
    echo "=== PQC readiness pass complete ==="
  fi

  echo "=== HyperShift TLS scanner complete (management + guest) ==="
  exit "${OVERALL_EXIT_CODE}"
fi

run_tls_scan
