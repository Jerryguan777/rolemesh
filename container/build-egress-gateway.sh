#!/bin/bash
# Build the RoleMesh egress gateway container image.
#
# The gateway is a separate image from the agent so the runtime layer
# of agents cannot import gateway code. It also lets operators update
# the gateway (e.g. roll a DNS-library CVE) without rebuilding agents.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="rolemesh-egress-gateway"
TAG="${1:-latest}"
CONTAINER_BUILDER="${CONTAINER_BUILDER:-${CONTAINER_RUNTIME:-docker}}"

echo "Building RoleMesh egress gateway image..."
echo "Image: ${IMAGE_NAME}:${TAG}"

${CONTAINER_BUILDER} build \
    -t "${IMAGE_NAME}:${TAG}" \
    -f "$SCRIPT_DIR/Dockerfile.egress-gateway" \
    "$PROJECT_ROOT"

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
