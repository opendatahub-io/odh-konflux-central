#!/usr/bin/env bash
# Stage filtered JUnit/logs then push to OCI once per PipelineRun (idempotent via .oci-upload-ok).
set -euo pipefail

TESTS_PAYLOAD_DIR="${TESTS_PAYLOAD_DIR:-}"
SCRIPTS_REPO_ROOT="${SCRIPTS_REPO_ROOT:-}"
OCI_ARTIFACT_REFERENCE="${OCI_ARTIFACT_REFERENCE:-}"
DOCKER_CONFIG="${DOCKER_CONFIG:-/tmp/rhoai-e2e-oci-docker-config}"
export DOCKER_CONFIG
OCI_SECRET_CONFIG="${OCI_SECRET_CONFIG:-/tmp/oci-secret-readonly/config.json}"
TESTS_CONFIG_PATH="${TESTS_CONFIG_PATH:-${SCRIPTS_REPO_ROOT}/config/rhoai-e2e-tests-config.yaml}"
# collect-diagnostics and publish-results are finally tasks and run in parallel.
WAIT_FOR_ARTIFACTS_SEC="${WAIT_FOR_ARTIFACTS_SEC:-90}"
WAIT_FOR_DIAGNOSTICS_SEC="${WAIT_FOR_DIAGNOSTICS_SEC:-600}"
DIAGNOSTICS_MARKER="${TESTS_PAYLOAD_DIR}/.collect-diagnostics-done"

if [[ -z "$TESTS_PAYLOAD_DIR" || -z "$SCRIPTS_REPO_ROOT" || -z "$OCI_ARTIFACT_REFERENCE" ]]; then
  echo "[ERROR] TESTS_PAYLOAD_DIR, SCRIPTS_REPO_ROOT, and OCI_ARTIFACT_REFERENCE are required" >&2
  exit 1
fi

mkdir -p "${TESTS_PAYLOAD_DIR}/results"

if [[ -f "$TESTS_PAYLOAD_DIR/.oci-upload-ok" ]]; then
  echo "[INFO] tests-payload already uploaded; skipping"
  exit 0
fi

_has_upload_files() {
  find "${TESTS_PAYLOAD_DIR}/results" -type f \( -name '*.xml' -o -name '*.log' -o -name '*.console.log' \) -print -quit 2>/dev/null | grep -q .
}

_wait_for_collect_diagnostics() {
  if [[ -f "$DIAGNOSTICS_MARKER" ]]; then
    return 0
  fi
  echo "[INFO] Waiting up to ${WAIT_FOR_DIAGNOSTICS_SEC}s for collect-diagnostics marker..."
  local waited=0
  while [[ "$waited" -lt "$WAIT_FOR_DIAGNOSTICS_SEC" ]]; do
    if [[ -f "$DIAGNOSTICS_MARKER" ]]; then
      echo "[INFO] collect-diagnostics finished after ${waited}s"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "[WARN] collect-diagnostics marker missing; diagnostic log may be omitted from OCI upload" >&2
}

_wait_for_collect_diagnostics

if ! _has_upload_files; then
  echo "[INFO] Waiting up to ${WAIT_FOR_ARTIFACTS_SEC}s for JUnit/triage artifacts (parallel finally tasks)..."
  waited=0
  while [[ "$waited" -lt "$WAIT_FOR_ARTIFACTS_SEC" ]]; do
    if _has_upload_files; then
      break
    fi
    sleep 2
    waited=$((waited + 2))
  done
fi

if ! _has_upload_files; then
  echo "[INFO] No JUnit/log files to upload"
  exit 0
fi

export TESTS_CONFIG_PATH
(
  cd "${SCRIPTS_REPO_ROOT}"
  python3 -m steps.stage_tests_payload_upload
) || {
  echo "[ERROR] stage_tests_payload_upload failed" >&2
  exit 1
}

STAGING_ROOT="${TESTS_PAYLOAD_DIR}/.upload-staging"
if [[ ! -d "$STAGING_ROOT" ]] || [[ -z "$(find "$STAGING_ROOT" -type f -print -quit 2>/dev/null || true)" ]]; then
  echo "[INFO] No staged JUnit/log files to upload"
  exit 0
fi

mkdir -p "$DOCKER_CONFIG"
if [[ -f "$OCI_SECRET_CONFIG" ]]; then
  cp "$OCI_SECRET_CONFIG" "${DOCKER_CONFIG}/config.json"
else
  echo "[WARNING] OCI push secret not mounted; push may fail (anonymous)"
fi

export OCI_GATE_SUBDIR=""
export OCI_TAG_EXPIRATION="${OCI_TAG_EXPIRATION:-30d}"
export ALWAYS_PASS="${ALWAYS_PASS:-false}"
cd "$STAGING_ROOT"
bash "${SCRIPTS_REPO_ROOT}/tekton/scripts/secure_push_oci_artifacts.sh"
touch "${TESTS_PAYLOAD_DIR}/.oci-upload-ok"
