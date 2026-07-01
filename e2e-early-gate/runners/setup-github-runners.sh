#!/bin/bash
# Sets up multiple self-hosted containerized GitHub Actions runners on an
# Ubuntu VM using Docker.
#
# Prerequisites: docker, curl, jq
# Environment:   GITHUB_TOKEN must be set (PAT with admin:org scope)
#
# The admin token is used only by this script to obtain short-lived
# registration/removal tokens. The containers themselves never see the
# admin PAT — they only receive a short-lived RUNNER_TOKEN.
#
# Usage:
#   GITHUB_TOKEN=ghp_xxx ./setup-github-runners.sh                   # start 2 runners
#   GITHUB_TOKEN=ghp_xxx ./setup-github-runners.sh --runners 5       # start 5 runners
#   GITHUB_TOKEN=ghp_xxx ./setup-github-runners.sh --teardown        # stop & deregister
#   GITHUB_TOKEN=ghp_xxx ./setup-github-runners.sh --status          # show status
#   GITHUB_TOKEN=ghp_xxx ./setup-github-runners.sh --logs 2          # view logs for runner 2

set -eo pipefail

# =============================================================================
# CONFIGURATION — override via environment variables or CLI flags
# =============================================================================

GITHUB_ORG="${GITHUB_ORG:-red-hat-data-services}"
RUNNER_GROUP="${RUNNER_GROUP:-Konflux}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,konflux,devops}"
HOST_NAME="${HOST_NAME:-devops-ci-host-1}"
RUNNER_NAME_PREFIX="${RUNNER_NAME_PREFIX:-${HOST_NAME}}"
RUNNER_COUNT="${RUNNER_COUNT:-2}"
RUNNER_EPHEMERAL="${RUNNER_EPHEMERAL:-false}"
RUNNER_VERSION="${RUNNER_VERSION:-2.335.1}"

CONTAINER_IMAGE="${CONTAINER_IMAGE:-github-runner:${RUNNER_VERSION}}"
CONTAINER_NAME_PREFIX="${CONTAINER_NAME_PREFIX:-gha-runner}"
CONTAINER_CPUS="${CONTAINER_CPUS:-2}"
CONTAINER_MEMORY="${CONTAINER_MEMORY:-4g}"
CONTAINER_NETWORK="${CONTAINER_NETWORK:-host}"
CONTAINER_EXTRA_ARGS="${CONTAINER_EXTRA_ARGS:-}"

# =============================================================================
# INTERNALS
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="start"
FORCE_BUILD=false
LOG_RUNNER_NUM=""

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage: GITHUB_TOKEN=<token> ./setup-github-runners.sh [OPTIONS]

Options:
  --runners N, -n N     Number of runners to create (default: 2)
  --group NAME          Runner group name (default: Konflux)
  --labels LABELS       Comma-separated runner labels (default: self-hosted,konflux)
  --prefix PREFIX       Runner name prefix (default: konflux-runner)
  --ephemeral           Enable ephemeral mode (single-job runners)
  --teardown, --stop    Stop and deregister all runners
  --status              Show runner status
  --logs [N]            View logs (all runners or specific runner N)
  --force-build         Rebuild container image even if it exists
  --help, -h            Show this help message

Environment variables:
  GITHUB_TOKEN          Required. PAT with admin:org scope
  GITHUB_ORG            GitHub organization (default: red-hat-data-services)
  RUNNER_VERSION        Runner binary version (default: 2.334.0)
  CONTAINER_CPUS        CPU limit per container (default: 2)
  CONTAINER_MEMORY      Memory limit per container (default: 4g)
  CONTAINER_EXTRA_ARGS  Additional docker run flags (e.g. --privileged)
EOF
  exit 0
}

# =============================================================================
# ARGUMENT PARSING
# =============================================================================

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runners|-n)
      RUNNER_COUNT="$2"; shift 2 ;;
    --group)
      RUNNER_GROUP="$2"; shift 2 ;;
    --labels)
      RUNNER_LABELS="$2"; shift 2 ;;
    --prefix)
      RUNNER_NAME_PREFIX="$2"; shift 2 ;;
    --ephemeral)
      RUNNER_EPHEMERAL="true"; shift ;;
    --teardown|--stop)
      ACTION="teardown"; shift ;;
    --status)
      ACTION="status"; shift ;;
    --logs)
      ACTION="logs"
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then
        LOG_RUNNER_NUM="$2"; shift
      fi
      shift ;;
    --force-build)
      FORCE_BUILD=true; shift ;;
    --help|-h)
      usage ;;
    *)
      die "Unknown option: $1. Use --help for usage." ;;
  esac
done

# =============================================================================
# GITHUB API HELPERS
# =============================================================================

github_api() {
  local method="$1" endpoint="$2"
  shift 2
  local url="https://api.github.com${endpoint}"
  local http_code body

  for attempt in 1 2 3; do
    body=$(curl -sSf -w '\n%{http_code}' \
      -X "${method}" \
      -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      "$@" \
      "${url}" 2>/dev/null) || true

    http_code=$(echo "${body}" | tail -1)
    body=$(echo "${body}" | sed '$d')

    case "${http_code}" in
      2[0-9][0-9]) echo "${body}"; return 0 ;;
      5[0-9][0-9])
        log "WARNING: GitHub API returned ${http_code}, retry ${attempt}/3..."
        sleep $((attempt * 2))
        ;;
      401) die "Authentication failed (401). Verify GITHUB_TOKEN is valid." ;;
      403)
        local required_scope="admin:org"
        local action_hint=""
        case "${endpoint}" in
          */actions/runners/registration-token)
            action_hint="Registering runners requires 'admin:org' scope." ;;
          */actions/runners/remove-token)
            action_hint="Removing runners requires 'admin:org' scope." ;;
          */actions/runners*)
            action_hint="Listing runners requires 'admin:org' scope." ;;
          *)
            action_hint="This endpoint may require 'admin:org' or 'read:org' scope." ;;
        esac
        die "Forbidden (403) on ${method} ${endpoint}. ${action_hint} Check your token scopes at: GitHub → Settings → Developer settings → Personal access tokens (classic)." ;;
      404) die "Not found (404). Check GITHUB_ORG='${GITHUB_ORG}'." ;;
      *)   die "GitHub API returned ${http_code}: ${body}" ;;
    esac
  done

  die "GitHub API call failed after 3 retries."
}

get_registration_token() {
  local resp
  resp=$(github_api POST "/orgs/${GITHUB_ORG}/actions/runners/registration-token")
  echo "${resp}" | jq -r '.token // empty'
}

get_removal_token() {
  local resp
  resp=$(github_api POST "/orgs/${GITHUB_ORG}/actions/runners/remove-token")
  echo "${resp}" | jq -r '.token // empty'
}

# =============================================================================
# PREREQUISITES
# =============================================================================

check_prerequisites() {
  local cmd
  for cmd in docker curl jq; do
    command -v "${cmd}" &>/dev/null || die "'${cmd}' is required but not installed."
  done

  [[ -n "${GITHUB_TOKEN:-}" ]] || die "GITHUB_TOKEN environment variable is not set."

  log "Validating GitHub token permissions..."
  local http_code
  http_code=$(curl -sSo /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/orgs/${GITHUB_ORG}" 2>/dev/null)

  case "${http_code}" in
    200) log "Token validated for org '${GITHUB_ORG}'." ;;
    401) die "Authentication failed (401). Verify GITHUB_TOKEN is valid." ;;
    403) die "Forbidden (403). Token may lack permissions or org requires SSO authorization." ;;
    404) die "Organization '${GITHUB_ORG}' not found (404)." ;;
    *)   die "Unexpected response (${http_code}) when validating token." ;;
  esac
}

# =============================================================================
# IMAGE BUILD
# =============================================================================

build_runner_image() {
  if docker image inspect "${CONTAINER_IMAGE}" &>/dev/null && [[ "${FORCE_BUILD}" != "true" ]]; then
    log "Image '${CONTAINER_IMAGE}' already exists. Use --force-build to rebuild."
    return 0
  fi

  log "Building runner image '${CONTAINER_IMAGE}' (runner v${RUNNER_VERSION})..."
  docker build \
    --build-arg "RUNNER_VERSION=${RUNNER_VERSION}" \
    -f "${SCRIPT_DIR}/Dockerfile.github-runner" \
    -t "${CONTAINER_IMAGE}" \
    "${SCRIPT_DIR}"
  log "Image built successfully."
}

# =============================================================================
# RUNNER LIFECYCLE
# =============================================================================

start_runners() {
  build_runner_image

  log "Obtaining registration token..."
  local reg_token
  reg_token=$(get_registration_token)
  [[ -n "${reg_token}" ]] || die "Failed to obtain registration token."

  local docker_gid=""
  if [[ -S /var/run/docker.sock ]]; then
    docker_gid=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)
    log "Docker socket GID: ${docker_gid}"
  else
    log "WARNING: Docker socket not found at /var/run/docker.sock. Container-based GitHub Actions jobs will not work."
  fi

  log "Starting ${RUNNER_COUNT} runner(s)..."
  local i container_name runner_name
  for i in $(seq 1 "${RUNNER_COUNT}"); do
    runner_name="${RUNNER_NAME_PREFIX}-runner-${i}"
    container_name="${CONTAINER_NAME_PREFIX}-${i}"

    if docker inspect "${container_name}" 2>/dev/null; then
      local state
      state=$(docker inspect --format '{{.State.Status}}' "${container_name}" 2>/dev/null || true)
      if [[ "${state}" == "running" ]]; then
        log "Runner '${runner_name}' already running (container: ${container_name}). Skipping."
        continue
      fi
      log "Removing stopped container '${container_name}'..."
      docker rm "${container_name}" 2>/dev/null || true
    fi

    # Docker-in-Docker path alignment: the runner passes its internal paths
    # (e.g. _work, externals) as host-path bind mounts when creating job
    # containers. Since the runner is itself a container, those paths don't
    # exist on the host. We fix this by giving each runner a unique base
    # directory on the host and mounting it at the same path in the container.
    # The entrypoint symlinks externals into this directory. Both the runner
    # and its job containers then resolve the same host path to the same files.
    local runner_dir="/opt/actions-runner-${i}"
    sudo mkdir -p "${runner_dir}"
    sudo chown -R 1001:1001 "${runner_dir}"

    log "Starting runner '${runner_name}' (container: ${container_name})..."
    # shellcheck disable=SC2086
    docker run -d \
      --name "${container_name}" \
      --hostname "${runner_name}" \
      --cpus "${CONTAINER_CPUS}" \
      --memory "${CONTAINER_MEMORY}" \
      --restart unless-stopped \
      --network "${CONTAINER_NETWORK}" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "${runner_dir}:${runner_dir}" \
      ${docker_gid:+--group-add "${docker_gid}"} \
      -e GITHUB_ORG="${GITHUB_ORG}" \
      -e RUNNER_TOKEN="${reg_token}" \
      -e RUNNER_NAME="${runner_name}" \
      -e RUNNER_GROUP="${RUNNER_GROUP}" \
      -e RUNNER_LABELS="${RUNNER_LABELS}" \
      -e RUNNER_EPHEMERAL="${RUNNER_EPHEMERAL}" \
      -e RUNNER_WORKDIR="${runner_dir}/_work" \
      ${CONTAINER_EXTRA_ARGS} \
      "${CONTAINER_IMAGE}" || {
        log "WARNING: Failed to start '${runner_name}'. Continuing with next runner."
        continue
      }

    log "Started runner '${runner_name}'."
  done

  echo ""
  show_status
}

stop_runners() {
  log "Obtaining removal token..."
  local remove_token
  remove_token=$(get_removal_token) || true

  if [[ -z "${remove_token}" ]]; then
    log "WARNING: Could not obtain removal token. Runners may remain registered as offline."
  fi

  log "Stopping ${RUNNER_COUNT} runner(s)..."
  local i container_name
  for i in $(seq 1 "${RUNNER_COUNT}"); do
    container_name="${CONTAINER_NAME_PREFIX}-${i}"

    if ! docker inspect "${container_name}" 2>/dev/null; then
      log "Container '${container_name}' not found. Skipping."
      continue
    fi

    # Inject the removal token so the entrypoint's cleanup trap can deregister
    if [[ -n "${remove_token}" ]]; then
      docker exec "${container_name}" \
        bash -c "export RUNNER_TOKEN='${remove_token}'" 2>/dev/null || true
    fi

    log "Stopping '${container_name}' (30s grace period for deregistration)..."
    docker stop --time 30 "${container_name}" 2>/dev/null || {
      log "WARNING: Graceful stop failed for '${container_name}', killing..."
      docker kill "${container_name}" 2>/dev/null || true
    }

    docker rm "${container_name}" 2>/dev/null || {
      log "WARNING: Failed to remove '${container_name}'."
    }

    log "Removed '${container_name}'."

    local runner_dir="/opt/actions-runner-${i}"
    if [[ -d "${runner_dir}" ]]; then
      sudo rm -rf "${runner_dir}"
      log "Cleaned up runner directory '${runner_dir}'."
    fi
  done

  log "All runners stopped."
}

show_status() {
  log "=== Container Status ==="
  docker ps -a --filter "name=${CONTAINER_NAME_PREFIX}" \
    --format "table {{.Names}}\t{{.Status}}\t{{.CreatedAt}}" 2>/dev/null || true

  echo ""
  log "=== GitHub Registered Runners ==="
  local resp runners
  resp=$(curl -sSf \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/orgs/${GITHUB_ORG}/actions/runners?per_page=100" 2>/dev/null) || {
    log "WARNING: Could not query GitHub API for registered runners."
    return 0
  }

  runners=$(echo "${resp}" | jq -r --arg prefix "${RUNNER_NAME_PREFIX}" \
    '.runners[] | select(.name | startswith($prefix)) | "\(.name)\t\(.status)\t\(.labels | map(.name) | join(","))"')

  if [[ -z "${runners}" ]]; then
    log "No runners matching prefix '${RUNNER_NAME_PREFIX}' found in org '${GITHUB_ORG}'."
  else
    printf "%-25s %-10s %s\n" "NAME" "STATUS" "LABELS"
    echo "${runners}" | while IFS=$'\t' read -r name status labels; do
      printf "%-25s %-10s %s\n" "${name}" "${status}" "${labels}"
    done
  fi
}

view_logs() {
  local runner_num="${1:-}"
  if [[ -n "${runner_num}" ]]; then
    local container_name="${CONTAINER_NAME_PREFIX}-${runner_num}"
    log "Showing logs for '${container_name}'..."
    docker logs --follow "${container_name}"
  else
    log "Showing last 20 lines from all runner containers..."
    local i container_name
    for i in $(seq 1 "${RUNNER_COUNT}"); do
      container_name="${CONTAINER_NAME_PREFIX}-${i}"
      if docker inspect "${container_name}" 2>/dev/null; then
        echo "--- ${container_name} ---"
        docker logs --tail 20 "${container_name}" 2>/dev/null || true
        echo ""
      fi
    done
  fi
}

# =============================================================================
# MAIN
# =============================================================================

main() {
  log "GitHub Actions Runner Setup"
  log "  Org:        ${GITHUB_ORG}"
  log "  Group:      ${RUNNER_GROUP}"
  log "  Labels:     ${RUNNER_LABELS}"
  log "  Runners:    ${RUNNER_COUNT}"
  log "  Ephemeral:  ${RUNNER_EPHEMERAL}"
  log "  Version:    ${RUNNER_VERSION}"
  log ""

  check_prerequisites

  case "${ACTION}" in
    start)    start_runners ;;
    teardown) stop_runners ;;
    status)   show_status ;;
    logs)     view_logs "${LOG_RUNNER_NUM}" ;;
  esac
}

main
