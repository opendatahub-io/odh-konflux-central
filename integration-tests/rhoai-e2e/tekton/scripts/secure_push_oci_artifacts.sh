#!/usr/bin/env bash
# Synced from red-hat-data-services/rhoai-konflux-tasks stepactions/secure-push-oci/0.1/secure-push-oci.yaml
# Inlined here so BVT/smoke upload steps do not add a second git StepAction
# resolver (Tekton prepare/place-scripts was observed stuck Init:0/2 for long periods).
set -euo pipefail

main() {
  IMAGE_REF_REGEX='^quay\.io/[a-zA-Z0-9_-]+(/[a-zA-Z0-9_-]+)+:[a-zA-Z0-9._-]+$'

  if [[ ! ${OCI_ARTIFACT_REFERENCE:-} =~ $IMAGE_REF_REGEX ]]; then
      echo -e "[ERROR]: provided OCI artifact reference '${OCI_ARTIFACT_REFERENCE:-}' is not in correct format 'quay.io/org/repo:tag'"
      return 1
  fi

  TAG="${OCI_ARTIFACT_REFERENCE##*:}"
  OCI_TAG_EXPIRATION="${OCI_TAG_EXPIRATION:-30d}"
  OCI_GATE_SUBDIR="${OCI_GATE_SUBDIR:-}"

  # Gate folders are siblings at the run artifact root (bvt/, smoke/, tier1/). After oras pull,
  # do not move an existing gate directory into the gate currently being uploaded.
  is_sibling_gate_dir() {
    case "$1" in
      bvt|smoke|tier1) return 0 ;;
      *) return 1 ;;
    esac
  }

  stage_gate_artifacts() {
    local gate="${OCI_GATE_SUBDIR}"
    [[ -z "$gate" ]] && return 0
    mkdir -p "$gate"
    shopt -s nullglob
    for item in *; do
      local base
      base="$(basename "$item")"
      [[ "$base" == "$gate" ]] && continue
      if [[ -d "$item" ]] && is_sibling_gate_dir "$base"; then
        continue
      fi
      case "$base" in
        .oci-upload-ok|leaktk-scan*.log) continue ;;
      esac
      mv -- "$item" "${gate}/"
    done
    shopt -u nullglob
  }

  TEMP_ANNOTATION_FILE="$(mktemp)"
  ORAS_ERR="$(mktemp)"
  trap 'rm -f "${TEMP_ANNOTATION_FILE}" "${ORAS_ERR}"' EXIT

  # Fetch the manifest annotations for the container
  set +e
  MANIFESTS_RAW="$(oras manifest fetch "$OCI_ARTIFACT_REFERENCE" 2>"$ORAS_ERR")"
  mf_rc=$?
  set -e
  ORAS_ERR_TEXT="$(cat "$ORAS_ERR" 2>/dev/null || true)"

  if [[ $mf_rc -ne 0 ]]; then
    if echo "$ORAS_ERR_TEXT" | grep -qiE 'not found|404|manifest unknown|NAME_UNKNOWN'; then
      MANIFESTS_ANNOTATIONS=""
    else
      echo -e "[ERROR]: oras manifest fetch failed for '$OCI_ARTIFACT_REFERENCE': ${ORAS_ERR_TEXT:-no stderr}" >&2
      return 1
    fi
  else
    MANIFESTS_ANNOTATIONS="$(printf '%s' "$MANIFESTS_RAW" | jq .annotations)"
  fi

  if [ "$MANIFESTS_ANNOTATIONS" == "" ] || [ "$MANIFESTS_ANNOTATIONS" == "null" ]; then
      # Create the annotations file, because it does not exist in the OCI artifact
      echo -e "[INFO]: provided OCI artifact tag '$OCI_ARTIFACT_REFERENCE' does not exist - will create it"
      jq -n --arg exp "$OCI_TAG_EXPIRATION" --arg title "Artifact storage for pipelinerun: $TAG" \
          '{"$manifest": {"quay.expires-after": $exp, "org.opencontainers.image.title": $title}}' > "${TEMP_ANNOTATION_FILE}"
  else
      echo -e "[INFO]: going to update existing content in '$OCI_ARTIFACT_REFERENCE'"
      # Keep the existing annotations file for further use
      jq -n --argjson manifest "$MANIFESTS_ANNOTATIONS" '{ "$manifest": $manifest }' > "${TEMP_ANNOTATION_FILE}"
      if ! oras pull "$OCI_ARTIFACT_REFERENCE" 2>"$ORAS_ERR"; then
        echo -e "[ERROR]: oras pull failed for '$OCI_ARTIFACT_REFERENCE': $(cat "$ORAS_ERR" 2>/dev/null || true)" >&2
        return 1
      fi
  fi

  stage_gate_artifacts

  # Scan the working directory using leaktk-scanner and remove problematic files
  log_filename="leaktk-scan-$(date +%s).log"
  leaktk-scanner scan --kind Files --resource . 2>> "$log_filename" | leaktk-remove-files . &>> "$log_filename"

  # Push the content to remote artifact storage
  attempt=1
  while ! oras push "$OCI_ARTIFACT_REFERENCE" --annotation-file "${TEMP_ANNOTATION_FILE}" ./:application/vnd.acme.rocket.docs.layer.v1+tar; do
      if [[ $attempt -ge 5 ]]; then
          echo -e "[ERROR]: oras push failed after $attempt attempts."
          return 1
      fi
      echo -e "[WARNING]: oras push failed (attempt $attempt). Retrying in 5 seconds..."
      sleep 5
      ((attempt++))
  done
  touch .oci-upload-ok
}

if [[ -e "/workspace/status" && $(cat /workspace/status) == "SKIP" ]]; then
    echo "Skipping this step as decided in previous steps.."
    exit 0
fi
if [ "${ALWAYS_PASS:-false}" == "true" ]; then
  main || true
else
  main
fi
