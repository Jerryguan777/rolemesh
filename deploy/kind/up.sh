#!/usr/bin/env bash
# Bring up a local kind cluster wired for RoleMesh (docs/21 §10.3).
#
# Steps:
#   1. inotify pre-check (kind nodes share the host's fs.inotify limits;
#      too low and pods crashloop with "too many open files").
#   2. create the kind cluster with the default CNI DISABLED (kindnet has
#      no NetworkPolicy — agent isolation would be a false green).
#   3. install Calico (NetworkPolicy-enforcing CNI).
#   4. kind load the four local images (single-arch amd64): orchestrator,
#      webui, egress-gateway, agent.
#   5. print the helm install command.
#
# It does NOT run `helm install` — that is the operator's explicit step so
# this script stays a pure environment bootstrap.
#
# Usage:
#   deploy/kind/up.sh            # build images first with the commands printed below
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CLUSTER_NAME="rolemesh"
NAMESPACE="rolemesh"

# Image tags loaded into the cluster. values-kind.yaml pins these :latest
# with imagePullPolicy: Never.
IMAGES=(
  "rolemesh-orchestrator:latest"
  "rolemesh-webui:latest"
  "rolemesh-egress-gateway:latest"
  "rolemesh-agent:latest"
)

# Calico manifest version. Pinned for reproducibility; bump deliberately.
CALICO_VERSION="v3.27.3"

err() { printf '\033[31m[up] %s\033[0m\n' "$*" >&2; }
log() { printf '\033[36m[up] %s\033[0m\n' "$*"; }

require() {
  command -v "$1" >/dev/null 2>&1 || { err "missing required tool: $1"; exit 1; }
}

require kind
require kubectl
require docker

# --- 1. inotify pre-check (docs/21 §8.3/§11) --------------------------------
# kind nodes are containers on the host kernel and share fs.inotify limits.
# uvicorn --reload, the kubelet, and several controllers each consume
# watches/instances; the common distro default (128 instances) is too low
# and surfaces as opaque crashloops. Warn loudly; do not auto-sudo.
check_inotify() {
  local want_watches=524288 want_instances=512
  local cur_watches cur_instances
  cur_watches="$(cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null || echo 0)"
  cur_instances="$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)"
  log "inotify: max_user_watches=$cur_watches max_user_instances=$cur_instances"
  if [ "$cur_watches" -lt "$want_watches" ] || [ "$cur_instances" -lt "$want_instances" ]; then
    err "inotify limits look low for a kind cluster. If pods crashloop with"
    err "'too many open files', raise them (root):"
    err "  sysctl -w fs.inotify.max_user_watches=$want_watches"
    err "  sysctl -w fs.inotify.max_user_instances=$want_instances"
    err "Persist in /etc/sysctl.d/99-inotify.conf. Continuing anyway."
  fi
}
check_inotify

# --- 2. create the cluster --------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  log "kind cluster '$CLUSTER_NAME' already exists — skipping create."
else
  log "creating kind cluster '$CLUSTER_NAME' (default CNI disabled)…"
  kind create cluster --config "$SCRIPT_DIR/cluster.yaml"
fi

# --- 3. install Calico ------------------------------------------------------
# kind --config disables kindnet; without a CNI, nodes stay NotReady. Calico
# enforces NetworkPolicy (the whole point — docs/21 §8.3).
if kubectl get daemonset -n kube-system calico-node >/dev/null 2>&1; then
  log "Calico already present — skipping install."
else
  log "installing Calico $CALICO_VERSION…"
  kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
  log "waiting for Calico to be ready…"
  kubectl -n kube-system rollout status daemonset/calico-node --timeout=180s
fi

log "waiting for nodes to be Ready…"
kubectl wait --for=condition=Ready nodes --all --timeout=180s

# --- 4. load images ---------------------------------------------------------
# kind load needs the images present in the host docker; build them first
# (see the printed commands below if any are missing).
missing=()
for img in "${IMAGES[@]}"; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    missing+=("$img")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  err "these images are not built locally:"
  for img in "${missing[@]}"; do err "  - $img"; done
  err ""
  err "Build them (single-arch amd64) from the repo root:"
  err "  container/build.sh                       # rolemesh-agent:latest"
  err "  docker build -f container/orchestrator.Dockerfile -t rolemesh-orchestrator:latest ."
  err "  docker build -f container/webui.Dockerfile         -t rolemesh-webui:latest ."
  err "  docker build -f container/Dockerfile.egress-gateway -t rolemesh-egress-gateway:latest ."
  err "then re-run this script."
  exit 1
fi

log "loading images into the kind cluster…"
for img in "${IMAGES[@]}"; do
  log "  kind load: $img"
  kind load docker-image "$img" --name "$CLUSTER_NAME"
done

# --- 5. namespace + PSA label + next steps ----------------------------------
log "creating namespace '$NAMESPACE' (PodSecurity restricted)…"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace "$NAMESPACE" \
  pod-security.kubernetes.io/enforce=restricted --overwrite || true

cat <<EOF

[up] Cluster '$CLUSTER_NAME' is ready with Calico and the RoleMesh images loaded.

Next:
  helm upgrade --install rolemesh "$REPO_ROOT/deploy/charts/rolemesh" \\
    -n $NAMESPACE \\
    -f "$REPO_ROOT/deploy/charts/rolemesh/values-kind.yaml"

  # watch startup
  kubectl -n $NAMESPACE get pods -w

  # confirm the CNI actually enforces NetworkPolicy (deny probe)
  helm test rolemesh -n $NAMESPACE

  # reach the webui
  kubectl -n $NAMESPACE port-forward svc/rolemesh-webui 8080:8080

Tear down with: deploy/kind/down.sh
EOF
