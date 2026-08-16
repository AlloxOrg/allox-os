#!/usr/bin/env bash
# Build the AIO runtime image executed inside the outer Kata VM.
#
# Usage:
#   ./build.sh                          # local tag allox/aio-runtime:v2
#   REGISTRY=your-registry.io/allox TAG=v2 ./build.sh --push
#   BASE_TAG=1.0.0.150 ./build.sh
#
# After build, configure allox:
#   allox config set defaults.image allox/aio-runtime:v2

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

BASE_TAG="${BASE_TAG:-1.0.0.150}"
REGISTRY="${REGISTRY:-allox}"
IMAGE_NAME="${IMAGE_NAME:-aio-runtime}"
TAG="${TAG:-v2}"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${TAG}"
PUSH=false

for arg in "$@"; do
  case "${arg}" in
    --push) PUSH=true ;;
    -h|--help)
      echo "Usage: REGISTRY=... TAG=... BASE_TAG=... $0 [--push]"
      exit 0
      ;;
  esac
done

echo "==> Pulling base image ghcr.io/agent-infra/sandbox:${BASE_TAG}"
if docker image inspect "ghcr.io/agent-infra/sandbox:${BASE_TAG}" >/dev/null 2>&1; then
  echo "    (local image found, skipping pull)"
else
  docker pull "ghcr.io/agent-infra/sandbox:${BASE_TAG}"
fi

echo "==> Building ${FULL_IMAGE}"
docker build \
  --build-arg "BASE_TAG=${BASE_TAG}" \
  -t "${FULL_IMAGE}" \
  -f Dockerfile \
  .

echo "==> Built ${FULL_IMAGE}"
docker images "${FULL_IMAGE}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}"

if [[ "${PUSH}" == "true" ]]; then
  echo "==> Pushing ${FULL_IMAGE}"
  docker push "${FULL_IMAGE}"
  echo "==> Configure allox:"
  echo "    allox config set defaults.image ${FULL_IMAGE}"
else
  echo ""
  echo "Local build complete. To push:"
  echo "  REGISTRY=your-registry.io/allox TAG=v2 $0 --push"
  echo ""
  echo "Configure allox:"
  echo "  allox config set defaults.image ${FULL_IMAGE}"
fi
