#!/bin/bash
# Build the RoleMesh agent container image (Python)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

IMAGE_NAME="rolemesh-agent"
TAG="${1:-latest}"
# Image-build tool (docker/podman/buildah). Not the OCI runtime —
# that is controlled by CONTAINER_RUNTIME (runc|runsc) in the Python
# orchestrator and applied per-container via ContainerSpec.runtime.
CONTAINER_BUILDER="${CONTAINER_BUILDER:-${CONTAINER_RUNTIME:-docker}}"

echo "Building RoleMesh agent container image (Python)..."
echo "Image: ${IMAGE_NAME}:${TAG}"

${CONTAINER_BUILDER} build -t "${IMAGE_NAME}:${TAG}" -f "$SCRIPT_DIR/Dockerfile" "$PROJECT_ROOT"

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"groupFolder\":\"test\",\"chatJid\":\"test@g.us\",\"isMain\":false}' | ${CONTAINER_BUILDER} run -i ${IMAGE_NAME}:${TAG}"
