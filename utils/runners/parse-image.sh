#!/bin/bash
# Parses an OCI image reference into REPO, TAG, and DIGEST.
# If TAG or DIGEST is absent, resolves the missing component via skopeo.
#
# Supported input formats:
#   quay.io/org/image:<tag>
#   quay.io/org/image@sha256:<digest>
#   quay.io/org/image:<tag>@sha256:<digest>
#
# Optional env vars:
#   SKOPEO_CREDS   - "user:password" passed to skopeo --creds (skips if unset)
#
# Usage:
#   bash parse-image.sh <image-reference>
#
# When sourced, $REPO / $TAG / $DIGEST are available in the caller's scope:
#   source parse-image.sh "quay.io/org/image:v1@sha256:abc"

set -eo pipefail

IMAGE="${1:-}"
if [[ -z "${IMAGE}" ]]; then
    echo "Usage: $0 <image-reference>" >&2
    exit 1
fi

# --- Parse: extract digest (everything after the first @) ---
if [[ "${IMAGE}" == *"@"* ]]; then
    DIGEST="${IMAGE#*@}"
    IMAGE_NO_DIGEST="${IMAGE%%@*}"
else
    DIGEST=""
    IMAGE_NO_DIGEST="${IMAGE}"
fi

# --- Parse: extract tag (last colon-suffix, guarded against registry host:port) ---
IMAGE_PATH="${IMAGE_NO_DIGEST#*/}"   # strip registry host (e.g. "quay.io")
if [[ "${IMAGE_PATH}" == *":"* ]]; then
    TAG="${IMAGE_NO_DIGEST##*:}"
    REPO="${IMAGE_NO_DIGEST%:*}"
else
    TAG=""
    REPO="${IMAGE_NO_DIGEST}"
fi

# --- Build skopeo credential args ---
SKOPEO_AUTH_ARGS=()
if [[ -n "${SKOPEO_CREDS:-}" ]]; then
    SKOPEO_AUTH_ARGS+=(--creds "${SKOPEO_CREDS}")
fi

# --- Resolve missing digest from tag via skopeo inspect ---
if [[ -z "${DIGEST}" && -n "${TAG}" ]]; then
    echo "[parse-image] Digest missing — resolving via skopeo for ${REPO}:${TAG} ..." >&2
    DIGEST=$(skopeo inspect "${SKOPEO_AUTH_ARGS[@]}" \
        --format '{{.Digest}}' \
        "docker://${REPO}:${TAG}")
    echo "[parse-image] Resolved digest: ${DIGEST}" >&2
fi

# --- Resolve missing tag by scanning all tags for a digest match ---
if [[ -z "${TAG}" && -n "${DIGEST}" ]]; then
    echo "[parse-image] Tag missing — scanning tags in ${REPO} for digest ${DIGEST} ..." >&2
    ALL_TAGS=$(skopeo list-tags "${SKOPEO_AUTH_ARGS[@]}" "docker://${REPO}" \
        | jq -r '.Tags[]')
    for candidate in ${ALL_TAGS}; do
        candidate_digest=$(skopeo inspect "${SKOPEO_AUTH_ARGS[@]}" \
            --format '{{.Digest}}' \
            "docker://${REPO}:${candidate}" 2>/dev/null || true)
        if [[ "${candidate_digest}" == "${DIGEST}" ]]; then
            TAG="${candidate}"
            echo "[parse-image] Resolved tag: ${TAG}" >&2
            break
        fi
    done
    if [[ -z "${TAG}" ]]; then
        echo "[parse-image] Warning: no tag found matching ${DIGEST} in ${REPO}" >&2
    fi
fi

echo "IMAGE:  ${IMAGE}"
echo "REPO:   ${REPO}"
echo "TAG:    ${TAG}"
echo "DIGEST: ${DIGEST}"
