#!/bin/bash
# Entrypoint for the GitHub Actions runner container.
#
# Handles runner configuration, execution, and graceful shutdown.
# The registration token is obtained by the host setup script and
# passed in — this container does NOT need a GitHub admin token.
#
# Required env vars:
#   RUNNER_TOKEN   - Short-lived registration token (obtained by setup script)
#   RUNNER_NAME    - Name to register this runner as
#   GITHUB_ORG     - GitHub organization name
#
# Optional env vars:
#   RUNNER_GROUP     - Runner group (default: Konflux)
#   RUNNER_LABELS    - Comma-separated labels (default: self-hosted,konflux)
#   RUNNER_EPHEMERAL - true/false (default: false)
#   RUNNER_WORKDIR   - Working directory (default: _work)

set -eo pipefail

# ---------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------
RUNNER_GROUP="${RUNNER_GROUP:-Konflux}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,konflux}"
RUNNER_EPHEMERAL="${RUNNER_EPHEMERAL:-false}"
RUNNER_WORKDIR="${RUNNER_WORKDIR:-_work}"

# ---------------------------------------------------------------
# Validation
# ---------------------------------------------------------------
missing=""
[[ -z "${RUNNER_TOKEN:-}" ]] && missing="${missing} RUNNER_TOKEN"
[[ -z "${RUNNER_NAME:-}" ]]  && missing="${missing} RUNNER_NAME"
[[ -z "${GITHUB_ORG:-}" ]]   && missing="${missing} GITHUB_ORG"
if [[ -n "${missing}" ]]; then
  echo "ERROR: Missing required environment variable(s):${missing}" >&2
  exit 1
fi

# ---------------------------------------------------------------
# Cleanup on shutdown
# ---------------------------------------------------------------
CLEANUP_DONE=0

cleanup() {
  if [[ "${CLEANUP_DONE}" -eq 1 ]]; then return; fi
  CLEANUP_DONE=1

  echo "Removing runner configuration for '${RUNNER_NAME}'..." >&2
  ./config.sh remove --token "${RUNNER_TOKEN}" 2>&1 \
    || echo "WARNING: config.sh remove failed (runner may already be removed)" >&2
}

trap cleanup SIGTERM SIGINT EXIT

# ---------------------------------------------------------------
# Docker-in-Docker: align externals path with the host-mounted dir
# so job containers can bind-mount the same path and find Node.js.
# ---------------------------------------------------------------
if [[ -n "${RUNNER_WORKDIR:-}" ]]; then
  RUNNER_BASE=$(dirname "${RUNNER_WORKDIR}")
  HOST_EXTERNALS="${RUNNER_BASE}/externals"
  LOCAL_EXTERNALS="/home/runner/actions-runner/externals"
  if [[ "${HOST_EXTERNALS}" != "${LOCAL_EXTERNALS}" ]]; then
    mkdir -p "${HOST_EXTERNALS}"
    if [[ -d "${LOCAL_EXTERNALS}" && ! -L "${LOCAL_EXTERNALS}" ]]; then
      cp -a "${LOCAL_EXTERNALS}/." "${HOST_EXTERNALS}/"
      rm -rf "${LOCAL_EXTERNALS}"
    fi
    ln -sfn "${HOST_EXTERNALS}" "${LOCAL_EXTERNALS}"
    echo "Linked externals: ${LOCAL_EXTERNALS} -> ${HOST_EXTERNALS}" >&2
  fi
fi

# ---------------------------------------------------------------
# Configure runner
# ---------------------------------------------------------------
echo "Configuring runner '${RUNNER_NAME}' for org '${GITHUB_ORG}'..." >&2
echo "  Group:     ${RUNNER_GROUP}" >&2
echo "  Labels:    ${RUNNER_LABELS}" >&2
echo "  Ephemeral: ${RUNNER_EPHEMERAL}" >&2

CONFIG_ARGS=(
  --url "https://github.com/${GITHUB_ORG}"
  --token "${RUNNER_TOKEN}"
  --name "${RUNNER_NAME}"
  --labels "${RUNNER_LABELS}"
  --runnergroup "${RUNNER_GROUP}"
  --work "${RUNNER_WORKDIR}"
  --unattended
  --replace
  --disableupdate
)

if [[ "${RUNNER_EPHEMERAL}" == "true" ]]; then
  CONFIG_ARGS+=(--ephemeral)
fi

./config.sh "${CONFIG_ARGS[@]}"

# ---------------------------------------------------------------
# Run
# ---------------------------------------------------------------
echo "Runner '${RUNNER_NAME}' is starting..." >&2
./bin/Runner.Listener run --startuptype service &
RUNNER_PID=$!
wait ${RUNNER_PID}
