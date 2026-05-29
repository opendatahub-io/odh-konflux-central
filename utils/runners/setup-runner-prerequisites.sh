#!/bin/bash
# Installs prerequisites inside the GitHub Actions runner container image.
#
# This script is COPYed into the Dockerfile and executed during image build.
# Add new dependencies here as workflows require them — the runner image
# will pick them up on the next rebuild (--force-build).
#
# Current prerequisites:
#   - Docker CLI   : needed for GitHub Actions jobs that use container: directives
#                    (e.g. stage-promoter runs inside quay.io/rhoai/rhoai-task-toolset:stage)

set -eo pipefail

ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64)  DOCKER_ARCH="x86_64" ;;
  aarch64) DOCKER_ARCH="aarch64" ;;
  *)       echo "Unsupported architecture: ${ARCH}" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------
# Docker CLI (client only, no daemon)
# ---------------------------------------------------------------
DOCKER_VERSION="${DOCKER_VERSION:-27.4.1}"
echo "Installing Docker CLI v${DOCKER_VERSION} (${DOCKER_ARCH})..."
curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_VERSION}.tgz" \
  | tar xz --strip-components=1 -C /usr/local/bin docker/docker
chmod +x /usr/local/bin/docker
docker --version

# ---------------------------------------------------------------
# Add more prerequisites below as needed
# ---------------------------------------------------------------

echo "Runner prerequisites installed successfully."
