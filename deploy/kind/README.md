# Local kind integration for RoleMesh (docs/21 §10.3)

Run the full RoleMesh stack on a local single-node [kind](https://kind.sigs.k8s.io/)
cluster with the K8s container-runtime backend (`ROLEMESH_CONTAINER_RUNTIME=k8s`).
This is the local equivalent of an RKE2 deploy and the gate for the P2/P3 work:
`helm install` end-to-end on kind, contract suite green on `--runtime=k8s`.

## Why a custom cluster

- **Calico, not kindnet.** kind's default CNI (kindnet) accepts
  `NetworkPolicy` objects but does NOT enforce them. RoleMesh agent
  isolation is entirely NetworkPolicy-based, so kindnet would be a silent
  false green. `cluster.yaml` sets `disableDefaultCNI: true` and `up.sh`
  installs Calico.
- **Service CIDR contract.** The gateway needs a static `ClusterIP` equal to
  `EGRESS_GATEWAY_DNS_IP` (a hard contract — see the chart). kind's default
  Service CIDR is `10.96.0.0/12`; `values-kind.yaml` pins `10.96.0.53`
  (kube-dns is `10.96.0.10`). Change both together if you change the CIDR.

## Prerequisites

- Docker (rootful), `kind`, `kubectl`, `helm`.
- `fs.inotify` limits raised (kind nodes share host kernel limits; `up.sh`
  warns if they look low). If pods crashloop with "too many open files":
  ```
  sudo sysctl -w fs.inotify.max_user_watches=524288
  sudo sysctl -w fs.inotify.max_user_instances=512
  ```

## Full loop

```sh
# 1. Build the four images (single-arch amd64), from the repo root:
container/build.sh                                                  # rolemesh-agent:latest
docker build -f container/orchestrator.Dockerfile  -t rolemesh-orchestrator:latest .
docker build -f container/webui.Dockerfile         -t rolemesh-webui:latest .
docker build -f container/Dockerfile.egress-gateway -t rolemesh-egress-gateway:latest .

# 2. Create the cluster, install Calico, load the images:
deploy/kind/up.sh

# 3. Install the chart with the kind values:
helm upgrade --install rolemesh deploy/charts/rolemesh \
  -n rolemesh \
  -f deploy/charts/rolemesh/values-kind.yaml

# 4. Wait for everything to be ready:
kubectl -n rolemesh get pods -w
kubectl -n rolemesh logs deploy/rolemesh-orchestrator -f   # "Infrastructure verified"

# 5. Confirm the CNI actually enforces NetworkPolicy (deny probe):
helm test rolemesh -n rolemesh

# 6. Reach the WebUI:
kubectl -n rolemesh port-forward svc/rolemesh-webui 8080:8080
#   -> http://localhost:8080  (dev admin: admin@example.com, seeded by the Job)

# Tear down:
deploy/kind/down.sh
```

## Verifying the contract by hand

```sh
# Gateway Service ClusterIP must equal EGRESS_GATEWAY_DNS_IP (10.96.0.53):
kubectl -n rolemesh get svc egress-gateway -o jsonpath='{.spec.clusterIP}'

# The four NetworkPolicies must exist by exact name:
kubectl -n rolemesh get networkpolicy

# The data PVC must be Bound:
kubectl -n rolemesh get pvc rolemesh-data
```

If the orchestrator refuses to start, its log line names the unmet
invariant (it runs the same `verify_infrastructure` the contract suite
gates on). Common causes: ClusterIP drift, a missing/renamed
NetworkPolicy, an unbound PVC, or a CNI that does not enforce policy.

## Running the contract suite against kind

```sh
# Point kubeconfig at the kind cluster (kind does this on create), then:
uv run pytest tests/container/contract/ -m integration --runtime=k8s
```

The suite verifies the running deployment; it never builds infrastructure
(docs/21 §9). Bring the chart up first.
