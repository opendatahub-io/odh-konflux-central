#!/usr/bin/env bash
# Pre-flight for MaaS group-test TLS step: confirm the pinned Quay digest exists
# before spending a Konflux PipelineRun. Usage:
#   ./scripts/verify-tls-scanner-image.sh
#   ./scripts/verify-tls-scanner-image.sh 'quay.io/opendatahub/tls-scanner@sha256:...'
set -euo pipefail

IMAGE="${1:-${TLS_SCANNER_IMAGE:-quay.io/opendatahub/tls-scanner@sha256:a815d178e081cfa6d776362b9b440fa969a6cd61aeeeaf7fb73f4b0b16e58d8d}}"

if [[ "${IMAGE}" != *@sha256:* ]]; then
  echo "ERROR: expected digest-pinned reference (image@sha256:...), got: ${IMAGE}" >&2
  exit 2
fi

DIGEST="${IMAGE#*@}"
REGISTRY_REPO="${IMAGE%@*}"

if [[ "${REGISTRY_REPO}" != quay.io/* ]]; then
  echo "ERROR: only quay.io/... references are supported, got: ${REGISTRY_REPO}" >&2
  exit 2
fi

PATH_ON_QUAY="${REGISTRY_REPO#quay.io/}"
MANIFEST_URL="https://quay.io/v2/${PATH_ON_QUAY}/manifests/${DIGEST}"

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