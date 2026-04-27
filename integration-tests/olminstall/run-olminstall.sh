#!/bin/bash
# Trigger (or attach to) the olminstall smoke pipeline in Konflux.
#
# - Ensures the ITS CR is applied (idempotent).
# - If an olminstall PipelineRun is already running, attaches to it.
# - Otherwise creates a fresh Snapshot to trigger a new run, then watches it.
# - Defaults to the latest Konflux-built FBCF image across all RHOAI apps;
#   narrow with --version (for --product rhoai) or override with --image.
# - Cleans up the test Snapshot on exit.
#
# Usage:
#   ./run-olminstall.sh                                        # latest FBCF across all RHOAI apps
#   ./run-olminstall.sh --image quay.io/rhoai/rhoai-fbc-fragment@sha256:abc123
#   ./run-olminstall.sh --app rhoai-fbc-fragment-ocp-421       # trigger on the real build app
#   ./run-olminstall.sh --konflux-repo https://github.com/you/fork.git --konflux-branch my-branch
#       # override Tekton clone of integration-test scripts (default: upstream main in pipeline)
#   ./run-olminstall.sh --channel beta
#       # override UPDATE_CHANNEL (default auto: odh-stable for ODH, stable-3.x for rhoai-v3*, else pipeline default)
#   ./run-olminstall.sh --product rhoai --version 3.5
#       # resolve latest FBCF image from the rhoai-v3-5* Konflux app (instead of ocp-421)

set -euo pipefail

NAMESPACE="rhoai-tenant"
APP="testops-playpen"
KONFLUX_UI="${KONFLUX_UI:-https://konflux-ui.apps.stone-prod-p02.hjvn.p1.openshiftapps.com}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SNAPSHOT_FILE="${SCRIPT_DIR}/test-snapshot.yaml"
ITS_FILE="${SCRIPT_DIR}/its-olminstall-rhoai-tenant.yaml"
IMAGE=""          # empty = fetch latest automatically
SNAPSHOT_NAME=""  # tracked for cleanup
PIPELINE_EXIT=0
KONFLUX_REPO_OVERRIDE=""
KONFLUX_BRANCH_OVERRIDE=""
UPDATE_CHANNEL_OVERRIDE=""
PRODUCT="rhoai"
VERSION=""  # e.g. "3.5" → lookup rhoai-v3-5* apps for latest FBC snapshot
ITS_APPLY_TMP=""
LOG_FILE=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Trigger and watch the olminstall smoke pipeline in Konflux.
By default uses the latest Konflux-built FBCF image across all RHOAI apps.

Options:
  --image IMAGE     FBCF container image to test (default: auto-fetch latest)
  --app APP         Konflux application to trigger against
                    (default: testops-playpen)
  --namespace NS    Konflux namespace
                    (default: rhoai-tenant)
  --konflux-repo URL  Git URL for odh-konflux-central fork (provides scripts and
                     pipeline). Merged into ITS spec.params; requires yq.
  --konflux-branch REF  Git branch/tag/SHA for the fork. Merged into ITS;
                     requires yq. Use with --konflux-repo.
  --channel NAME     OLM update channel to pass as UPDATE_CHANNEL (e.g. stable, beta, fast-3.x, odh-stable)
                    Default auto: odh-stable for --product odh; stable-3.x for rhoai-v3*; else pipeline/ITS default
  --product NAME    Product stream (rhoai|odh). Default: rhoai
  --version VER     Resolve FBCF image from a specific RHOAI release stream
                    (e.g. 3.5, 3.4, 3.4-ea.2). Valid only with --product rhoai.
                    Default: latest across all rhoai-v* apps
  --rhoai-version VER  Deprecated alias for --version
  -h, --help        Show this help
EOF
}

require_arg_value() {
  if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == --* ]]; then
    echo "❌ Missing value for $1"
    usage
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)         require_arg_value "$1" "${2:-}"; IMAGE="$2"; shift 2 ;;
    --app)           require_arg_value "$1" "${2:-}"; APP="$2"; shift 2 ;;
    --namespace|-n)  require_arg_value "$1" "${2:-}"; NAMESPACE="$2"; shift 2 ;;
    --konflux-repo)  require_arg_value "$1" "${2:-}"; KONFLUX_REPO_OVERRIDE="$2"; shift 2 ;;
    --konflux-branch) require_arg_value "$1" "${2:-}"; KONFLUX_BRANCH_OVERRIDE="$2"; shift 2 ;;
    --channel)       require_arg_value "$1" "${2:-}"; UPDATE_CHANNEL_OVERRIDE="$2"; shift 2 ;;
    --product)       require_arg_value "$1" "${2:-}"; PRODUCT="$2"; shift 2 ;;
    --version|--rhoai-version) require_arg_value "$1" "${2:-}"; VERSION="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [[ "${PRODUCT}" != "rhoai" && "${PRODUCT}" != "odh" ]]; then
  echo "❌ --product must be one of: rhoai, odh"
  exit 1
fi
if [[ -n "${VERSION}" && "${PRODUCT}" != "rhoai" ]]; then
  echo "--version is supported only with --product rhoai"
  exit 1
fi

# ── Apply product-specific pipeline params ────────────────────────────────────
# Both rhoai and odh use the rhoai-tenant/testops-playpen sandbox ITS for manual
# runs.  Product-specific params (operator name, namespace, component) are
# injected at ITS-apply time below.
ODH_PARAM_OVERRIDES=""
if [[ "${PRODUCT}" == "odh" ]]; then
  ODH_PARAM_OVERRIDES="yes"
fi
echo "Product: ${PRODUCT}  Namespace: ${NAMESPACE}  App: ${APP}"

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "── Cleaning up ──"
  if [[ -n "${SNAPSHOT_NAME}" ]]; then
    oc delete snapshot "${SNAPSHOT_NAME}" -n "${NAMESPACE}" --ignore-not-found &>/dev/null \
      && echo "  Deleted Snapshot ${SNAPSHOT_NAME}" || true
  fi
  [[ -n "${ITS_APPLY_TMP}" && -f "${ITS_APPLY_TMP}" ]] && rm -f "${ITS_APPLY_TMP}"
  [[ -n "${LOG_FILE}" && -f "${LOG_FILE}" ]] && rm -f "${LOG_FILE}"
}
trap cleanup EXIT

# Tekton v1 PipelineRun: never use status.conditions[0] — order is not stable.
# Read the condition with type=="Succeeded" (status True/False/Unknown).
pr_succeeded_state() {
  oc get pipelinerun "${1}" -n "${2}" -o json 2>/dev/null | jq -r '
    ((.status.conditions // []) | map(select(.type=="Succeeded")) | first) as $c
    | if $c == null then "Unknown\t"
      else ($c.status // "Unknown") + "\t" + ($c.reason // "")
      end
  '
}

# ── Check login ────────────────────────────────────────────────────────────────
if ! oc whoami &>/dev/null; then
  echo "❌ Not logged in. Run: oc login --server=<api-url> --web"
  exit 1
fi
echo "✓ Logged in as $(oc whoami)"

# ── Verify this is a Konflux cluster (has IntegrationTestScenario CRD) ─────────
KONFLUX_SERVER="https://api.stone-prod-p02.hjvn.p1.openshiftapps.com:6443"
if ! oc api-resources --api-group=appstudio.redhat.com 2>/dev/null | grep -q "IntegrationTestScenario"; then
  echo ""
  echo "⚠️  The current cluster ($(oc whoami --show-server)) does not have the"
  echo "   Konflux IntegrationTestScenario CRD (appstudio.redhat.com)."
  echo "   This script must run against the Konflux tenant cluster."
  if [[ -t 0 ]]; then
    read -r -p "   Log in to ${KONFLUX_SERVER} now? [Y/n] " _ans
    _ans="${_ans:-Y}"
  else
    _ans="Y"
  fi
  if [[ "${_ans}" =~ ^[Yy]$ ]]; then
    echo "   Running: oc login --server=${KONFLUX_SERVER} --web"
    oc login --server="${KONFLUX_SERVER}" --web
    if ! oc api-resources --api-group=appstudio.redhat.com 2>/dev/null | grep -q "IntegrationTestScenario"; then
      echo "❌ Still no IntegrationTestScenario CRD after login. Aborting."
      exit 1
    fi
    echo "✓ Re-logged in as $(oc whoami) on Konflux cluster"
  else
    echo "❌ Aborting — not connected to a Konflux cluster."
    exit 1
  fi
fi

# ── Check for an already-running olminstall PipelineRun for THIS app ──────────
# Only reuse a run that targets the same application (APP) so that concurrent
# users running against different apps/products don't accidentally cross-attach.
echo "Checking for running olminstall PipelineRun (app: ${APP})..."
PR=$(oc get pipelineruns -n "${NAMESPACE}" -o json 2>/dev/null \
  | jq -r --arg app "${APP}" '
      .items[]
      | select(.metadata.name | test("olminstall"))
      | select(.status.completionTime == null)
      | select(
          (.metadata.labels["appstudio.openshift.io/application"] // "") == $app
          or (.spec.params // [] | map(select(.name == "SNAPSHOT")) | length == 0)
        )
      | .metadata.name' \
  | head -1 || true)

if [[ -n "${PR}" ]]; then
  echo "↪ Found running PipelineRun for app '${APP}': ${PR} — attaching..."
else
  # ── Resolve FBCF image ───────────────────────────────────────────────────────
  if [[ -n "${IMAGE}" ]]; then
    echo "✓ Using provided image: ${IMAGE}"
  elif [[ "${PRODUCT}" == "rhoai" && -n "${VERSION}" ]]; then
    APP_PREFIX="rhoai-v${VERSION//./-}"
    echo "Resolving latest FBCF image for RHOAI ${VERSION} (apps matching ${APP_PREFIX}*)..."
    # Resolve exact app names first (fast), then query per-app with label selector (fast).
    # Scanning all snapshots without a label filter is too slow for large namespaces.
    MATCHING_APPS=$(oc get applications -n rhoai-tenant \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
      | tr ' ' '\n' \
      | grep -E "^${APP_PREFIX}(-|$)" \
      || true)
    if [[ -z "${MATCHING_APPS}" ]]; then
      echo "❌ No Konflux application found matching ${APP_PREFIX}* in rhoai-tenant"
      exit 1
    fi
    IMAGE=""; BEST_TS=""; RESOLVED_APP=""
    while IFS= read -r _app; do
      [[ -z "${_app}" ]] && continue
      _result=$(oc get snapshots -n rhoai-tenant \
        -l "appstudio.openshift.io/application=${_app}" -o json 2>/dev/null \
        | jq -r '
            [ .items[]
              | { ts: .metadata.creationTimestamp,
                  img: (.spec.components[]?
                        | select(.containerImage | test("rhoai-fbc-fragment@"))
                        | .containerImage) }
              | select(.img)
            ] | if length > 0 then sort_by(.ts) | last | [.ts, .img] | join("\t") else "" end' \
        || true)
      [[ -z "${_result}" ]] && continue
      _ts="${_result%%	*}"; _img="${_result##*	}"
      if [[ -z "${BEST_TS}" || "${_ts}" > "${BEST_TS}" ]]; then
        IMAGE="${_img}"; BEST_TS="${_ts}"; RESOLVED_APP="${_app}"
      fi
    done <<< "${MATCHING_APPS}"
    if [[ -z "${IMAGE}" ]]; then
      echo "❌ No FBCF snapshot found for RHOAI ${VERSION} (searched ${APP_PREFIX}*)"
      exit 1
    fi
    echo "✓ RHOAI ${VERSION} FBCF image: ${IMAGE} (from ${RESOLVED_APP})"
  elif [[ "${PRODUCT}" == "rhoai" ]]; then
    echo "Fetching latest FBCF image across all RHOAI apps (highest version)..."
    # Resolve app names first (fast), then query per-app with label selector (fast).
    ALL_RHOAI_APPS=$(oc get applications -n rhoai-tenant \
      -o jsonpath='{.items[*].metadata.name}' 2>/dev/null \
      | tr ' ' '\n' \
      | grep -E "^rhoai-v" \
      || true)
    IMAGE=""; BEST_TS=""; RESOLVED_APP=""
    while IFS= read -r _app; do
      [[ -z "${_app}" ]] && continue
      _result=$(oc get snapshots -n rhoai-tenant \
        -l "appstudio.openshift.io/application=${_app}" -o json 2>/dev/null \
        | jq -r '
            [ .items[]
              | { ts: .metadata.creationTimestamp,
                  img: (.spec.components[]?
                        | select(.containerImage | test("rhoai-fbc-fragment@"))
                        | .containerImage) }
              | select(.img)
            ] | if length > 0 then sort_by(.ts) | last | [.ts, .img] | join("\t") else "" end' \
        || true)
      [[ -z "${_result}" ]] && continue
      _ts="${_result%%	*}"; _img="${_result##*	}"
      if [[ -z "${BEST_TS}" || "${_ts}" > "${BEST_TS}" ]]; then
        IMAGE="${_img}"; BEST_TS="${_ts}"; RESOLVED_APP="${_app}"
      fi
    done <<< "${ALL_RHOAI_APPS}"
    if [[ -n "${IMAGE}" ]]; then
      echo "✓ Latest FBCF image: ${IMAGE} (from ${RESOLVED_APP})"
    else
      IMAGE=""
      echo "⚠ Could not fetch latest image — falling back to pinned image in test-snapshot.yaml"
    fi
  elif [[ "${PRODUCT}" == "odh" ]]; then
    ODH_CATALOG_REPO="quay.io/opendatahub/opendatahub-operator-catalog"
    ODH_CATALOG_TAG="odh-stable"
    echo "Fetching latest ODH catalog snapshot from open-data-hub-tenant..."
    IMAGE=$(oc get snapshots -n open-data-hub-tenant -o json 2>/dev/null \
      | jq -r '
          [ .items[]
            | select(.metadata.labels["appstudio.openshift.io/application"] == "opendatahub-builds")
            | { ts: .metadata.creationTimestamp,
                img: (.spec.components[]?
                      | select(.containerImage | test("opendatahub-operator-catalog@|odh-operator-catalog@"))
                      | .containerImage) }
            | select(.img)
          ] | sort_by(.ts) | last | .img // empty' || true)
    if [[ -z "${IMAGE}" ]]; then
      echo "  No snapshots found (likely no access to open-data-hub-tenant)"
      echo "  Resolving from ${ODH_CATALOG_REPO}:${ODH_CATALOG_TAG} via skopeo..."
      if command -v skopeo &>/dev/null; then
        ODH_DIGEST=$(skopeo inspect --no-tags "docker://${ODH_CATALOG_REPO}:${ODH_CATALOG_TAG}" 2>/dev/null \
          | jq -r '.Digest // empty')
        if [[ -n "${ODH_DIGEST}" ]]; then
          IMAGE="${ODH_CATALOG_REPO}@${ODH_DIGEST}"
        fi
      fi
      if [[ -z "${IMAGE}" ]]; then
        echo "  skopeo unavailable or inspect failed — using tag reference"
        IMAGE="${ODH_CATALOG_REPO}:${ODH_CATALOG_TAG}"
      fi
    fi
    echo "✓ Latest ODH catalog image: ${IMAGE}"
  fi

  # Auto-select channel unless --channel was provided explicitly.
  if [[ -z "${UPDATE_CHANNEL_OVERRIDE}" && "${PRODUCT}" == "odh" ]]; then
    UPDATE_CHANNEL_OVERRIDE="odh-stable"
    echo "✓ Auto-selected channel: ${UPDATE_CHANNEL_OVERRIDE} (product=${PRODUCT})"
  elif [[ -z "${UPDATE_CHANNEL_OVERRIDE}" && "${RESOLVED_APP:-}" == rhoai-v3-* ]]; then
    UPDATE_CHANNEL_OVERRIDE="stable-3.x"
    echo "✓ Auto-selected channel: ${UPDATE_CHANNEL_OVERRIDE} (from ${RESOLVED_APP})"
  fi

  # ── Ensure ITS CR is applied (idempotent) ───────────────────────────────────
  NEED_YQ=""
  [[ -n "${KONFLUX_REPO_OVERRIDE}" || -n "${KONFLUX_BRANCH_OVERRIDE}" || -n "${UPDATE_CHANNEL_OVERRIDE}" || -n "${ODH_PARAM_OVERRIDES}" ]] && NEED_YQ="yes"

  echo "Ensuring IntegrationTestScenario is applied..."
  if [[ -n "${NEED_YQ}" ]]; then
    if ! command -v yq &>/dev/null; then
      echo "❌ yq (https://github.com/mikefarah/yq) is required for --konflux-repo / --konflux-branch / --channel / --product odh."
      exit 1
    fi
    ITS_APPLY_TMP="$(mktemp)"

    # Build a dynamic list of params to delete — only remove what we will re-add.
    DEL_NAMES=()
    [[ -n "${KONFLUX_REPO_OVERRIDE}" ]]   && DEL_NAMES+=("SCRIPTS_REPO_URL")
    [[ -n "${KONFLUX_BRANCH_OVERRIDE}" ]] && DEL_NAMES+=("SCRIPTS_REPO_REVISION")
    [[ -n "${UPDATE_CHANNEL_OVERRIDE}" ]] && DEL_NAMES+=("UPDATE_CHANNEL")
    if [[ -n "${ODH_PARAM_OVERRIDES}" ]]; then
      DEL_NAMES+=("OPERATOR_NAME" "OPERATOR_NAMESPACE" "FBCF_COMPONENT_NAME")
    fi

    if [[ ${#DEL_NAMES[@]} -gt 0 ]]; then
      DEL_EXPR=$(printf ' or .name == "%s"' "${DEL_NAMES[@]}")
      DEL_EXPR="${DEL_EXPR:4}"  # strip leading " or "
      yq e "del(.spec.params[] | select(${DEL_EXPR}))" "${ITS_FILE}" > "${ITS_APPLY_TMP}"
    else
      cp "${ITS_FILE}" "${ITS_APPLY_TMP}"
    fi

    if [[ -n "${KONFLUX_REPO_OVERRIDE}" ]]; then
      YQ_SCRIPTS_URL="${KONFLUX_REPO_OVERRIDE}" \
        yq e '.spec.params += [{"name":"SCRIPTS_REPO_URL","value":strenv(YQ_SCRIPTS_URL)}]' -i "${ITS_APPLY_TMP}"
      YQ_RESOLVER_URL="${KONFLUX_REPO_OVERRIDE}" \
        yq e '(.spec.resolverRef.params[] | select(.name == "url")).value = strenv(YQ_RESOLVER_URL)' -i "${ITS_APPLY_TMP}"
    fi
    if [[ -n "${KONFLUX_BRANCH_OVERRIDE}" ]]; then
      YQ_SCRIPTS_REV="${KONFLUX_BRANCH_OVERRIDE}" \
        yq e '.spec.params += [{"name":"SCRIPTS_REPO_REVISION","value":strenv(YQ_SCRIPTS_REV)}]' -i "${ITS_APPLY_TMP}"
      YQ_RESOLVER_REV="${KONFLUX_BRANCH_OVERRIDE}" \
        yq e '(.spec.resolverRef.params[] | select(.name == "revision")).value = strenv(YQ_RESOLVER_REV)' -i "${ITS_APPLY_TMP}"
    fi
    if [[ -n "${UPDATE_CHANNEL_OVERRIDE}" ]]; then
      YQ_UPDATE_CHANNEL="${UPDATE_CHANNEL_OVERRIDE}" \
        yq e '.spec.params += [{"name":"UPDATE_CHANNEL","value":strenv(YQ_UPDATE_CHANNEL)}]' -i "${ITS_APPLY_TMP}"
    fi
    if [[ -n "${ODH_PARAM_OVERRIDES}" ]]; then
      yq e '.spec.params += [{"name":"OPERATOR_NAME","value":"opendatahub-operator"}]' -i "${ITS_APPLY_TMP}"
      yq e '.spec.params += [{"name":"OPERATOR_NAMESPACE","value":"opendatahub-operators"}]' -i "${ITS_APPLY_TMP}"
      yq e '.spec.params += [{"name":"FBCF_COMPONENT_NAME","value":"odh-operator-catalog"}]' -i "${ITS_APPLY_TMP}"
    fi
    echo "  ITS overrides: resolverRef=${KONFLUX_REPO_OVERRIDE:-<default>}@${KONFLUX_BRANCH_OVERRIDE:-<default>}" \
         " SCRIPTS_REPO=${KONFLUX_REPO_OVERRIDE:-<default>}@${KONFLUX_BRANCH_OVERRIDE:-<default>}" \
         " UPDATE_CHANNEL=${UPDATE_CHANNEL_OVERRIDE:-<pipeline default>}" \
         " PRODUCT=${PRODUCT}"
    # Pipe through grep to suppress harmless Warning lines; check oc exit code via PIPESTATUS.
    oc apply -n "${NAMESPACE}" -f "${ITS_APPLY_TMP}" 2>&1 | grep -v "^Warning" >&2
    [[ ${PIPESTATUS[0]} -ne 0 ]] && { echo "❌ ITS apply failed"; exit 1; }
  else
    oc apply -n "${NAMESPACE}" -f "${ITS_FILE}" 2>&1 | grep -v "^Warning" >&2
    [[ ${PIPESTATUS[0]} -ne 0 ]] && { echo "❌ ITS apply failed"; exit 1; }
  fi
  echo "✓ ITS ready"

  # ── Create Snapshot (patch app/image/component on the fly, file is never modified) ─
  SNAPSHOT_YAML=$(sed "s|application:.*|application: ${APP}|" "${SNAPSHOT_FILE}")
  [[ -n "${IMAGE}" ]] && SNAPSHOT_YAML=$(echo "${SNAPSHOT_YAML}" \
    | sed "s|containerImage:.*|containerImage: ${IMAGE}|")
  if [[ -n "${ODH_PARAM_OVERRIDES}" ]]; then
    SNAPSHOT_YAML=$(echo "${SNAPSHOT_YAML}" \
      | sed 's|name: rhoai-fbc-fragment-ocp-421|name: odh-operator-catalog|')
  fi
  echo "Creating Snapshot to trigger pipeline (app: ${APP})..."
  SNAPSHOT_NAME=$(echo "${SNAPSHOT_YAML}" | oc create -n "${NAMESPACE}" -f - \
    -o jsonpath='{.metadata.name}')
  echo "✓ Snapshot: ${SNAPSHOT_NAME}"

  # ── Wait for the PipelineRun to appear ───────────────────────────────────────
  echo "Waiting for PipelineRun to start..."
  for i in $(seq 1 24); do
    PR=$(oc get pipelineruns -n "${NAMESPACE}" \
      --sort-by=.metadata.creationTimestamp -o json 2>/dev/null \
      | jq -r '[ .items[] | select(.metadata.name | test("olminstall")) ] | last | .metadata.name' \
      || true)
    [[ -n "${PR}" && "${PR}" != "null" ]] && break
    echo "  waiting... (${i}/24)"
    sleep 5
  done

  if [[ -z "${PR}" || "${PR}" == "null" ]]; then
    echo "❌ PipelineRun did not appear after 2m. Check Konflux:"
    echo "   ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/activity/pipelineruns"
    exit 1
  fi
fi

# ── Print Konflux UI link ─────────────────────────────────────────────────────
echo ""
echo "PipelineRun : ${PR}"
echo "Logs        : tkn pipelinerun logs ${PR} -n ${NAMESPACE} -f"
echo "Konflux UI  : ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
echo ""

# ── Wait for pipeline to leave Pending/Resolving before streaming ─────────────
WAIT_DEADLINE=$(($(date +%s) + 300))
WAIT_START=$(date +%s)
echo "Waiting for pipeline to start running..."
while [ "$(date +%s)" -lt "$WAIT_DEADLINE" ]; do
  IFS=$'\t' read -r _CSTAT CREASON <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
  case "${CREASON}" in
    ""|PipelineRunPending|ResolvingPipelineRef)
      ELAPSED=$(( $(date +%s) - WAIT_START ))
      echo "  $(date +%H:%M:%S)  ${CREASON:-pending} (${ELAPSED}s)"
      sleep 10 ;;
    *)
      echo "  $(date +%H:%M:%S)  ${CREASON:-starting} — ready to stream"
      break ;;
  esac
done
if [ "$(date +%s)" -ge "$WAIT_DEADLINE" ]; then
  echo "❌ Pipeline still pending after 5m. Check Konflux:"
  echo "   ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
  PIPELINE_EXIT=1
  exit 1
fi

# ── Stream logs (tkn) or poll status ─────────────────────────────────────────
LOG_FILE="$(mktemp -t olminstall-run.XXXXXX)"
chmod 600 "${LOG_FILE}"
ts_prefix() { while IFS= read -r line; do printf '[%s] %s\n' "$(date +%H:%M:%S)" "$line"; done; }
if command -v tkn &>/dev/null; then
  echo "Streaming logs via tkn (Ctrl-C to detach, pipeline keeps running)..."
  tkn pipelinerun logs "${PR}" -n "${NAMESPACE}" -f 2>&1 | ts_prefix | tee "${LOG_FILE}" || true
  # tkn can return before the PipelineRun CR flips to terminal; wait on type=Succeeded.
  POST_LOG_DEADLINE=$(($(date +%s) + 300))
  while [ "$(date +%s)" -lt "${POST_LOG_DEADLINE}" ]; do
    IFS=$'\t' read -r CSTAT CREASON <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
    case "${CSTAT}" in
      True) break ;;
      False)
        echo "❌ Pipeline failed (${CREASON:-Failed})"
        oc get pipelinerun "${PR}" -n "${NAMESPACE}" -o json \
          | jq -r '(.status.conditions // []) | map(select(.type=="Succeeded")) | first | .message // empty' 2>/dev/null || true
        PIPELINE_EXIT=1
        break ;;
      *) sleep 3 ;;
    esac
  done
else
  echo "tkn not found — polling status (install tkn for live logs)"
  echo "  https://github.com/tektoncd/cli/releases"
  echo ""
  MAX_WAIT_SECONDS=5400
  POLL_DEADLINE=$(($(date +%s) + MAX_WAIT_SECONDS))
  while [ "$(date +%s)" -lt "${POLL_DEADLINE}" ]; do
    IFS=$'\t' read -r CSTAT CREASON <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
    echo "  $(date +%H:%M:%S)  succeeded-condition: ${CSTAT}  reason: ${CREASON:-?}"
    case "${CSTAT}" in
      True)
        echo "✅ Pipeline succeeded"
        break ;;
      False)
        echo "❌ Pipeline failed (${CREASON:-Failed})"
        oc get pipelinerun "${PR}" -n "${NAMESPACE}" -o json \
          | jq -r '(.status.conditions // []) | map(select(.type=="Succeeded")) | first | .message // empty' 2>/dev/null || true
        PIPELINE_EXIT=1
        break ;;
    esac
    sleep 15
  done
  if [ "$(date +%s)" -ge "${POLL_DEADLINE}" ]; then
    echo "❌ Polling timed out before pipeline reached a terminal state (${MAX_WAIT_SECONDS}s)"
    PIPELINE_EXIT=1
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
IFS=$'\t' read -r FINAL_CSTAT FINAL_STATUS <<< "$(pr_succeeded_state "${PR}" "${NAMESPACE}")"
OPERATOR_VERSION=$(sed -n 's/.*Operator version[[:space:]]*:[[:space:]]*\([^[:space:]]*\).*/\1/p' "${LOG_FILE}" 2>/dev/null | tail -1 || true)

echo ""
echo "═══════════════════════════════════════════════════════════"
echo " Summary"
echo "═══════════════════════════════════════════════════════════"
echo "  Pipeline  : ${PR}  [${FINAL_STATUS:-unknown}]"
[[ -n "${OPERATOR_VERSION}" ]] && echo "  Operator  : ${OPERATOR_VERSION}"
echo "  Konflux UI: ${KONFLUX_UI}/ns/${NAMESPACE}/applications/${APP}/pipelineruns/${PR}"
echo "═══════════════════════════════════════════════════════════"

if [[ "${FINAL_CSTAT}" != "True" ]]; then
  PIPELINE_EXIT=1
fi
exit "${PIPELINE_EXIT}"
