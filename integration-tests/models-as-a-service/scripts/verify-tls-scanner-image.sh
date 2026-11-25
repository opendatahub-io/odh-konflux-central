#!/usr/bin/env bash
# Pre-flight: confirm the tls-scanner reference resolves on Quay before a group-test run.
# Default image is read from pr-group-testing-pipeline.yaml (tls-scanner-image param).
# Usage:
#   ./scripts/verify-tls-scanner-image.sh
#   ./scripts/verify-tls-scanner-image.sh 'quay.io/opendatahub/tls-scanner@sha256:...'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_YAML="${SCRIPT_DIR}/../pr-group-testing-pipeline.yaml"

read_pipeline_default() {
  if [[ ! -f "${PIPELINE_YAML}" ]]; then
    echo "ERROR: pipeline YAML not found: ${PIPELINE_YAML}" >&2
    return 1
  fi
  awk '
    $0 ~ /^[[:space:]]*- name: tls-scanner-image$/ { found=1; next }
    found && $0 ~ /^[[:space:]]*default:/ {
      sub(/^[[:space:]]*default:[[:space:]]*"/, "")
      sub(/".*$/, "")
      print
      exit
    }
    found && $0 ~ /^[[:space:]]*- name:/ { exit 1 }
  ' "${PIPELINE_YAML}"
}

IMAGE="${1:-${TLS_SCANNER_IMAGE:-}}"
if [[ -z "${IMAGE}" ]]; then
  IMAGE="$(read_pipeline_default)" || exit 2
  if [[ -z "${IMAGE}" ]]; then
    echo "ERROR: could not read tls-scanner-image default from ${PIPELINE_YAML}" >&2
    exit 2
  fi
fi

if [[ "${IMAGE}" != quay.io/* ]]; then
  echo "ERROR: only quay.io/... references are supported, got: ${IMAGE}" >&2
  exit 2
fi

PATH_ON_QUAY="${IMAGE#quay.io/}"
if [[ "${PATH_ON_QUAY}" == *@sha256:* ]]; then
  REPO="${PATH_ON_QUAY%@*}"
  REF="${PATH_ON_QUAY#*@}"
else
  REPO="${PATH_ON_QUAY%%:*}"
  REF="${PATH_ON_QUAY#*:}"
  if [[ -z "${REF}" || "${REF}" == "${PATH_ON_QUAY}" ]]; then
    echo "ERROR: expected quay.io/repo:tag or quay.io/repo@sha256:..., got: ${IMAGE}" >&2
    exit 2
  fi
fi

MANIFEST_URL="https://quay.io/v2/${REPO}/manifests/${REF}"
code="$(curl -s -o /dev/null -w '%{http_code}' \
  -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
  "${MANIFEST_URL}")"

if [[ "${code}" != "200" ]]; then
  echo "FAIL: Quay returned HTTP ${code} for ${IMAGE}" >&2
  echo "      (Konflux ErrImagePull is expected when this check fails.)" >&2
  exit 1
fi

echo "OK: manifest exists on Quay for ${IMAGE}"
exit 0