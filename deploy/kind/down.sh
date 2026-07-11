#!/usr/bin/env bash
# Delete the RoleMesh kind cluster (docs/21 §10.3).
#
# This removes the whole cluster (and everything helm installed into it).
# It does NOT remove the local docker images — kind load copied them into
# the node, but `docker rmi` of the originals is left to you.
set -euo pipefail

CLUSTER_NAME="rolemesh"

if ! command -v kind >/dev/null 2>&1; then
  echo "[down] kind not found — nothing to do." >&2
  exit 0
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "[down] deleting kind cluster '$CLUSTER_NAME'..."
  kind delete cluster --name "$CLUSTER_NAME"
  echo "[down] done."
else
  echo "[down] no kind cluster named '$CLUSTER_NAME' — nothing to do."
fi
