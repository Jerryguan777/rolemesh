# Deploying RoleMesh on Rancher RKE2 (production)

Status: P3 (docs/21 §10.2, §12 step S3). Companion to the Helm chart at
`deploy/charts/rolemesh/` and the local kind loop at `deploy/kind/`.

This doc covers what is RKE2-specific. The chart itself, its three value
files, and the hard contract with `verify_infrastructure` are documented in
the chart's `values.yaml` and `docs/21-container-runtime-decoupling.md`.

## 0. The one-paragraph model

RoleMesh runs as a single-namespace deployment: a single-replica
orchestrator (Recreate strategy) that spawns agent **Pods** through the K8s
API, a dual-faced egress gateway exposed by a Service with a **static
ClusterIP**, a webui, four NetworkPolicies that fence agents to
gateway-only egress, and a shared data PVC. The orchestrator does NOT
create any of this — at startup it VERIFIES the chart's promises and
refuses to run if they do not hold (fail-closed).

## 1. Cluster prerequisites

| Requirement | Why | How on RKE2 |
|---|---|---|
| CNI enforces NetworkPolicy | agent isolation is policy-based | RKE2 ships Canal (Calico + Flannel) by default — it enforces. Cilium also fine. Do NOT run the `none` CNI. |
| PodSecurity `restricted` on the namespace | gateway/webui/orchestrator securityContexts target it | label the namespace (below) |
| A free Service-CIDR address for the gateway | static ClusterIP == `EGRESS_GATEWAY_DNS_IP` | RKE2 default Service CIDR is `10.43.0.0/16`; pick a free address e.g. `10.43.0.53` |
| Storage for the data PVC | shared agent/orchestrator data | RWX (Longhorn RWX / NFS) preferred; else `storage.mode=rwo-colocated` |
| kubelet `podPidsLimit` | pids limit is node-level, not a pod field | set in the RKE2 kubelet config (below) |

### 1.1 Discover the Service CIDR / a free address

```sh
kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}'   # e.g. 10.43.0.10
```

kube-dns sits inside the Service CIDR; pick a nearby unused address for
`egressGateway.clusterIP`. The API server rejects an out-of-CIDR value at
apply time, so a wrong guess fails fast rather than silently.

Two more rules when picking:

* **One address per RoleMesh instance.** ClusterIPs are cluster-global,
  not namespace-scoped — a second instance in another namespace needs its
  own free address. The per-environment values file doubles as the
  allocation ledger.
* **Prefer the low band of the CIDR** (e.g. `10.43.0.x`): K8s allocates
  dynamic ClusterIPs from the high band and reserves the low band for
  static assignment (ServiceIPStaticSubrange), so low picks rarely collide.

Pre-verify a candidate without touching cluster state — `created (server
dry run)` means it is free; `provided IP is already allocated` means pick
another:

```sh
kubectl -n rolemesh create service clusterip probe \
  --tcp=53:53 --clusterip=10.43.0.53 --dry-run=server
```

### 1.2 Label the namespace PSA restricted

```sh
kubectl create namespace rolemesh
kubectl label namespace rolemesh pod-security.kubernetes.io/enforce=restricted --overwrite
```

### 1.3 kubelet podPidsLimit (pids limit is node-level — docs/21 §8/§11)

Pod-level pids limits are a kubelet setting, not a pod field, so the chart
cannot set it; a fork-bomb in one agent can otherwise exhaust the node's
pid space. On every RKE2 agent node that will run agent pods, add to
`/etc/rancher/rke2/config.yaml`:

```yaml
kubelet-arg:
  - "pod-max-pids=512"
```

then restart the node's RKE2 service (`systemctl restart rke2-agent`). The
orchestrator logs a startup warning reminding operators of this.

### 1.4 Pre-install checklist

One command per prerequisite; every failure here would otherwise surface
later as a fail-closed startup or a scheduling-dependent flake. Run before
the first `helm install` on any new cluster or namespace:

```sh
# 1. Service CIDR + a free gateway address (see §1.1 for the two rules):
kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}'
kubectl -n rolemesh create service clusterip probe --tcp=53:53 \
  --clusterip=<candidate> --dry-run=server

# 2. NetworkPolicy RBAC (helm must be able to create the four policies):
kubectl auth can-i create networkpolicies -n rolemesh

# 3. CNI enforces NetworkPolicy — objects being accepted proves nothing
#    (kindnet/plain-flannel accept and ignore). The real proof is the
#    deny probe after install: helm test (§5.1).

# 4. Storage class for the data PVC (RWX preferred, else §2.2):
kubectl get storageclass

# 5. Private registry only — the pull secret exists in the namespace:
kubectl -n rolemesh get secret <pull-secret-name>
```

### 1.5 gVisor (optional, docs/21 §6.4)

If coworkers request the `runsc` OCI runtime, register a RuntimeClass and
set `orchestrator.runtimeClass` to its name. RKE2 can run gVisor via a
containerd runtime handler; see the gVisor + containerd docs. kind cannot
run gVisor — this is RKE2-only and gated by the `k8s-prod-only` test
marker.

## 2. Storage

### 2.1 RWX (preferred)

Install Longhorn (RWX) or an NFS StorageClass and set:

```yaml
storage:
  mode: rwx
  storageClass: "longhorn-rwx"   # or your NFS class
  size: 50Gi
```

The single data PVC is mounted by the orchestrator and subPath-mounted into
agent pods on any node.

### 2.2 RWO fallback (no RWX available)

```yaml
storage:
  mode: rwo-colocated
  storageClass: "longhorn"
  size: 50Gi
```

The data PVC is ReadWriteOnce; the orchestrator mounts it directly (the
scheduler pins it to the PVC's node), and the agent pods the orchestrator
spawns mount the same RWO PVC and co-locate automatically (a second node
attaching blocks with a Multi-Attach error).

## 3. Secrets

Do NOT ship the dev-default secrets to production. Generate real values:

```sh
# Fernet key for the per-tenant LLM credential vault:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# HMAC / signing secrets:
openssl rand -hex 32   # x3 for egressTokenSecret, wsTicketSecret, rolemeshTokenSecret
```

Either set them in a values override (e.g. a sealed values file kept out of
git) or manage the Secret out-of-band and point the chart at it:

```yaml
secrets:
  create: false
  existingSecret: "rolemesh-secrets"   # must carry the same keys (see secret.yaml)
```

`EGRESS_TOKEN_SECRET` is the load-bearing one: the orchestrator and the
gateway both mount it; a mismatch means every agent identity token fails
verification. The chart injects the SAME Secret key into both, so they
cannot drift when the chart manages the Secret.

## 4. Database

Bundled Postgres is a dev/PoC convenience and is NOT PSA-restricted
compatible. In production, point at a managed/external Postgres:

```yaml
postgres:
  enabled: false
  externalUrl: "postgresql://rolemesh:CHANGEME@pg.internal:5432/rolemesh"
  # BYPASSRLS pool for cross-tenant maintenance (RLS rollout); defaults to
  # externalUrl if omitted, acceptable only if that role can do DDL.
  externalAdminUrl: "postgresql://rolemesh_admin:CHANGEME@pg.internal:5432/rolemesh"
```

When `postgres.enabled=false`, widen `networkPolicy.orchestratorExtraEgress`
to reach the external DB if it is outside the cluster pod network:

```yaml
networkPolicy:
  orchestratorExtraEgress:
    - cidr: 10.0.0.0/8
      ports: [{port: 5432, protocol: TCP}]
```

## 5. Install

```sh
helm upgrade --install rolemesh deploy/charts/rolemesh \
  -n rolemesh \
  -f my-production-values.yaml      # your overrides on top of values.yaml
```

Minimum required overrides: `egressGateway.clusterIP`, real `secrets.*`,
external `postgres`, a real `storage.storageClass` — start from the
skeleton `deploy/charts/rolemesh/values-production.yaml` (copy per
environment, fill every CHANGEME).

### 5.1 Verify enforcement (deny probe)

```sh
helm test rolemesh -n rolemesh
```

The probe pod (labeled `rolemesh.io/role=agent`) must be BLOCKED from
reaching the public internet. A pass means the CNI is enforcing the agent
NetworkPolicies. Re-run after any CNI change — the startup verify checks
that policy OBJECTS exist, not that they are enforced (docs/21 §4.2).

### 5.2 Private registry: agent pods are BARE PODS

Platform mutation policies that auto-inject `imagePullSecrets` (Kyverno
and friends) typically match Deployments/StatefulSets. Chart-templated
components are covered — but **agent pods are created directly by the
orchestrator through the K8s API as bare Pods; no admission mutation ever
sees them.** Do not rely on injection: set `image.pullSecret` (see
`values-production.yaml`) — the chart forwards it to the orchestrator as
`ROLEMESH_K8S_IMAGE_PULL_SECRET` and every spawned agent pod carries it.

Beware the false green: agent pulls can succeed WITHOUT credentials on a
node that already has the image cached (pulled earlier by a mutated
Deployment pod). That state is fragile — new/replaced nodes and kubelet
image GC break it at random. Acceptance check on a node that has never
run RoleMesh (or after `crictl rmi` of the agent image there):

```sh
kubectl -n rolemesh run pull-probe --restart=Never --rm -it \
  --image=<registry>/<repo>/rolemesh-agent:<tag> \
  --overrides='{"spec":{"nodeName":"<cold-node>","imagePullSecrets":[{"name":"<pull-secret-name>"}]}}' \
  --command -- /bin/true
```

## 6. The hard contract (why startup can fail-closed)

The orchestrator's `verify_infrastructure` (read-only, at every startup)
asserts, by exact name/value:

- the four NetworkPolicies exist; `agent-default-deny` / `agent-allow-egress`
  select `rolemesh.io/role=agent`; `gateway-policy` / `orchestrator-policy`
  do NOT select agent pods;
- the `egress-gateway` Service exists and its ClusterIP equals
  `EGRESS_GATEWAY_DNS_IP`;
- the gateway answers `/healthz` on that ClusterIP:3001;
- NATS is reachable;
- the `rolemesh-data` PVC is Bound;
- the orchestrator ServiceAccount may create/delete/get/list/watch pods and
  get pods/log (SelfSubjectAccessReview).

If any fails, the orchestrator refuses to enter the ready state and the log
line names the unmet invariant. This is by design (fail-closed, docs/21 §1):
a half-configured cluster never silently runs unprotected agents.

### 6.1 RBAC footprint

The orchestrator gets a pods-only namespaced Role + RoleBinding, plus a
minimal ClusterRole/Binding granting `get` on ONLY the install namespace
(verify calls `read_namespace`, which a namespaced Role cannot satisfy —
namespaces are cluster-scoped). No other cluster-wide permission is
granted.

## 7. Upgrades

```sh
helm upgrade rolemesh deploy/charts/rolemesh -n rolemesh -f my-production-values.yaml
```

- The orchestrator uses `strategy: Recreate` — the old pod terminates fully
  before the new one starts (single stateful replica must not overlap,
  docs/21 §10.2). Expect a brief control-plane gap during upgrades;
  in-flight agent pods keep running (they are not owned by the Deployment).
- The seed Job re-runs as a `post-upgrade` hook (idempotent schema
  create/admin seed).
