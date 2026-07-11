# Container Runtime Decoupling: `ROLEMESH_CONTAINER_RUNTIME=docker|k8s`

Status: design rev4 (2026-06-12), review-amended. Baseline: `main@be953c5` (+#85).

Goal: the same business code switches between Docker and Kubernetes via one
environment variable. Local Ubuntu (amd64) validates both docker mode and kind
mode; production targets Rancher RKE2 via a Helm chart.

## 1. Core thesis (rev3)

rev1/rev2 applied "declarative infrastructure + the app only verifies" to the
K8s side while letting the Docker side keep imperatively creating bridges,
launching the gateway, and discovering the DNS IP at runtime inside the
business process. rev3 applies the same principle symmetrically:

1. **Static infrastructure sinks into the deployment layer.** Networks,
   gateway, NATS, Postgres are declared by docker compose (local) / Helm
   (production). Application code never creates them; at startup it only
   verifies invariants and refuses to start when they do not hold
   (fail-closed). No degradation, no self-bootstrap, no repair.
2. **The orchestrator joins the network stack** (compose service / K8s
   Deployment). Local and production topologies become isomorphic; the
   "host view vs container view" translation layer (`host.docker.internal`,
   loopback rewriting, ExtraHosts host-gateway) loses its premise and is
   deleted wholesale.
3. **EC=off mode and the hybrid run mode are removed** (decided 2026-06-12).
   `EGRESS_CONTROL_ENABLE` and all its branches go away — this reverts the
   EC=off fallback that PR #84 just restored. Rationale: a fallback should be
   a deployment-time choice (another compose profile), not a runtime branch in
   business code. We are pre-production with no data; this is the only
   low-cost window to collapse the branch.
4. The only imperative container operation kept in application code is the
   per-job creation/destruction of agent sandboxes — dynamic, per-request,
   genuinely the application's job.

Startup-ordering problems do not disappear; they become **explicit** (rev4):
from "implicit code execution order" to "an explicit startup contract", at
the cost of a new degraded-startup mode for the gateway (§5).

## 2. Design principles

- Business code depends on contracts, not mechanisms.
- Symmetric declarative: both runtimes' static infrastructure is declared by
  deployment artifacts; the app start-up check is read-only and fail-closed.
- Single topology: orchestrator and agents share one network stack; all
  cross-service access uses service names. There is no second viewpoint,
  hence no viewpoint-translation code.
- Single code path: no EC switch, no deployment-shape branches. Different
  shapes are different compose profiles / values files.
- Contract tests are the executable definition of the contract: one
  parameterized suite runs on both runtimes with zero `if runtime == ...`
  in test bodies.

## 3. Mechanism mapping

| Contract | Docker (compose declares) | K8s (Helm declares) |
|---|---|---|
| Agent has no direct egress | `internal: true` network | default-deny egress NetworkPolicy |
| Gateway exists, dual-faced | compose service on agent-net + egress-net | Deployment + Service (single NIC, policy splits inside/outside) |
| Gateway DNS address | compose ipam fixed IP -> `EGRESS_GATEWAY_DNS_IP` | Service ClusterIP -> same config |
| Agent DNS forced through gateway | `HostConfig.Dns=[configured IP]` | `dnsPolicy: None` + `dnsConfig.nameservers` |
| Request-level identity | signed token, in-band | identical, zero change |
| Credential resolution | gateway -> NATS RPC -> orchestrator | identical, zero change |
| NATS/DB reachability | compose service names | Service DNS names |
| Startup order | gateway depends only on NATS; orchestrator `verify` retries gateway healthz | readiness probe + verify retry (same semantics) |
| Gateway resolver upstream | Docker embedded DNS (127.0.0.11) | kube-dns (gateway's own resolv.conf) |
| Sandbox lifecycle | `docker run` equivalent (aiodocker) | bare Pod, `restartPolicy: Never` |
| Diagnostic log stream | container log (stderr) | pod log (stdout/stderr merged, §8) |
| Orphan cleanup | name prefix + image allowlist | label selector + image allowlist |
| Resource limits | Memory/NanoCpus/PidsLimit | `resources.limits` (pids is kubelet-level, §10) |
| Hardening | CapDrop/ReadonlyRootfs/no-new-privileges/tmpfs | securityContext + emptyDir(Memory) |
| gVisor | `spec.runtime=runsc` per sandbox | `runtimeClassName: gvisor` |
| Metadata protection | internal bridge (bare IP) + /etc/hosts blackhole (domains) | default-deny NetworkPolicy + gateway DNS allowlist |
| Shared storage | orchestrator and agent bind the same host dir (path translation, §7) | shared PVC + subPath (same translation layer) |

## 4. Code design

### 4.1 Configuration

`CONTAINER_RUNTIME = os.environ.get("ROLEMESH_CONTAINER_RUNTIME", "docker")`

| Variable | Mode | Meaning |
|---|---|---|
| `ROLEMESH_CONTAINER_RUNTIME` | both | `docker` \| `k8s` |
| `EGRESS_GATEWAY_DNS_IP` | both | static: compose fixed IP / Service ClusterIP; replaces runtime discovery |
| `NATS_URL` etc. | both | always service names; no localhost rewriting |
| `ROLEMESH_HOST_DATA_DIR` | docker | host path of DATA_DIR (DooD translation, §7) |
| `ROLEMESH_K8S_NAMESPACE` / `_DATA_PVC` / `_IMAGE_PULL_SECRET` / `_RUNTIME_CLASS` | k8s | as rev2 |

Removed: `EGRESS_CONTROL_ENABLE`, `CONTAINER_HOST_GATEWAY` and all derived logic.

### 4.2 ContainerRuntime protocol (final shape)

```python
class ContainerRuntime(Protocol):
    name: str
    async def ensure_available(self) -> None: ...      # API reachable + version
    async def verify_infrastructure(self) -> None: ... # verify deployment promises (read-only, fail-closed)
    async def run(self, spec: ContainerSpec) -> ContainerHandle: ...
    async def stop(self, name: str) -> None: ...
    async def cleanup_orphans(self, ...) -> list[str]: ...
    async def list_live(self, prefix: str) -> set[str]: ...  # running names; reaper liveness oracle (read-only)
    async def close(self) -> None: ...
```

No `provision_*`, no `get_network_info`, no EgressGatewayProvider /
HostAccessPolicy — their premises are gone.

`verify_infrastructure()`:

- Docker: agent-net exists and `Internal=true`; egress-net exists; gateway
  healthy (`/healthz`); `EGRESS_GATEWAY_DNS_IP` matches the gateway's actual
  agent-net address; NATS reachable.
- K8s (rev4): four NetworkPolicies exist and selectors match (object-level,
  milliseconds); PVC Bound; gateway Service healthz; RBAC self-check
  (SelfSubjectAccessReview). The deny *probe* (a pod whose egress must fail,
  guarding against CNIs that silently ignore NetworkPolicy) is NOT run at
  every startup — pod scheduling + image pull would drag startup into tens of
  seconds. It lives in `helm test` (once per install) and contract test
  T-NET. Cost: a CNI swapped after install is unguarded (rare op; NOTES).

### 4.3 compute_egress_routing collapses to single-path config read

After the EC branch is removed the function reads configuration only; docker
and k8s run the same code. The `warn_missing_dns` fail-open path is deleted:
a missing `EGRESS_GATEWAY_DNS_IP` is a configuration error — refuse to start.

### 4.4 K8sRuntime (rev2 carry-over, essentials)

`container/k8s_runtime.py`, optional dependency `kubernetes_asyncio` (lazy
import). ContainerSpec -> bare Pod (`restartPolicy: Never`,
`automountServiceAccountToken: false`, `enableServiceLinks: false`, labels
`rolemesh.io/role=agent`, `rolemesh.io/managed-by=orchestrator`); hardening
mapped field-by-field to securityContext; tmpfs -> `emptyDir(medium=Memory,
sizeLimit)`; `wait()` = watch + resourceVersion resume + periodic read
fallback; name conflict = delete-then-create (matches docker
create_or_replace); orphan cleanup = label selector + image allowlist.

### 4.5 Orchestrator containerization

- New `container/orchestrator.Dockerfile` (python 3.12-slim + uv sync).
- Compose mounts: `./src -> /app/src` (dev hot-reload), `./data -> /app/data`,
  `/var/run/docker.sock` (docker mode only; sole use is spawning agent
  sandboxes).
- Debug: debugpy on 5678 instead of attaching to a host process.
- Orchestrator joins agent-net (reach NATS/gateway) and egress-net (its own
  outbound traffic, e.g. LLM safety calls).
- evaluation CLI (docker mode only, decided): one-off container
  `docker compose run --rm orchestrator python -m rolemesh.evaluation ...`;
  its gateway self-bootstrap call becomes verify-only.
- webui image (rev4): `src/webui` is its own FastAPI process and needs its
  own Dockerfile (multi-stage: node builds `web/dist` -> runtime layer);
  `WEB_UI_DIST` becomes env-injectable absolute path.
- mount-allowlist (rev4): `MOUNT_ALLOWLIST_PATH` becomes env-configurable;
  compose binds `~/.config/rolemesh` (ro); K8s uses a ConfigMap.
- `orchestration/remote_control.py` is deleted (decided): its
  `start_remote_control` entry has zero callers, so the state file its
  `restore_remote_control` reads can never exist — dead code; its premise
  (operating the host) is dissolved by containerization anyway.

## 5. Gateway degraded startup (rev4: fixes the startup deadlock)

Problem (review finding, code-verified): the gateway fetches the rule
snapshot over NATS at startup and refuses to start without it
(`gateway.py` "refusing to start"); the snapshot responder lives in the
orchestrator. rev3's `orchestrator depends_on gateway(healthy)` makes that
circular: gateway waits for the snapshot, the snapshot waits for the
orchestrator, the orchestrator waits for gateway health — deadlock.

Fix — the gateway starts degraded:

- healthz returns 200 as soon as NATS is connected, without the snapshot;
- the policy plane starts **default-deny** and a background task retries the
  snapshot until it succeeds, then switches to the normal policy. This
  borrows the *shape* of the MCP-registry graceful pattern, with two
  differences: "empty registry" semantics become "deny all", and an actual
  retry loop is added (the MCP pattern only refills via change events, which
  for rules could mean staying deny-all forever — not acceptable).
- compose: gateway depends only on NATS; the orchestrator's
  `verify_infrastructure` retries gateway healthz with a timeout.
- The deny window is acceptable on cold start (no agents exist before the
  orchestrator finishes starting). When the gateway alone restarts while
  agents are running, in-flight agent traffic is briefly denied until the
  snapshot retry succeeds — fail-closed and accepted.

## 6. Network and security model

### 6.1 K8s NetworkPolicies (Helm templates, 4)

1. `agent-default-deny`: role=agent, Ingress+Egress all denied (standalone so
   the "default deny" invariant is independently verifiable).
2. `agent-allow-egress`: only agent -> gateway (53/udp+tcp, 3001, 3128) and
   -> NATS (4222). kube-dns is NOT allowed.
3. `gateway-policy`: ingress only agent/orchestrator; egress NATS (4222 —
   credential RPC and snapshot), 53/80/443 and values-declared ports.
4. `orchestrator-policy`: egress -> K8s API, Postgres, NATS, gateway,
   external HTTPS (its own LLM calls).

### 6.2 Identity and credentials (rev2 carry-over)

Token in-band, stateless verification at the gateway, zero per-runtime
difference. `EGRESS_TOKEN_SECRET` is delivered by compose env / K8s Secret to
orchestrator and gateway, never to agents. Real LLM keys exist only in DB
(Fernet) and the orchestrator's decryption path; the gateway fetches
per-request via NATS RPC with a TTL cache. Token TTL must cover K8s cold
start (values-validated lower bound).

### 6.3 DNS (rev4-corrected)

rev3's "both sides listen on 1053" does not hold on Docker: compose `ports:`
only affects host publishing — container-to-container traffic reaches the
actually-bound port, and `HostConfig.Dns`/resolv.conf has no port syntax. Per
the §3 mapping philosophy each side uses its own mechanism:

- Docker: resolver listens on 53, keeps the single `NET_BIND_SERVICE` cap.
- K8s: container listens on 1053, Service maps 53 -> 1053; the namespace is
  fully PSA `restricted`.

Internal-name resolution (rev5). Internal names (`nats`, `egress-gateway`,
and `*.cluster.local` on K8s) resolve via the platform's own resolver, by
different routes per runtime:

- Docker: the agent's resolver is the embedded DNS (127.0.0.11), which
  answers container names locally and forwards only external names to the
  gateway. Internal names never reach the gateway resolver — no exemption
  needed. (This is why the empty-allowlist default works on Docker but
  broke on K8s the first time a full agent round-trip ran there.)
- K8s: `dnsPolicy: None` pins the agent's resolver straight at the gateway,
  so the gateway itself EXEMPTS internal names from the allowlist and
  forwards them to kube-dns. It reads both the internal-name set and the
  kube-dns address from its OWN `/etc/resolv.conf` (the gateway keeps the
  default ClusterFirst dnsPolicy), so the same code is correct on both
  runtimes with no docker/k8s fork — `src/rolemesh/egress/dns_internal.py`.
  Only suffixes within the cluster domain are exempt; an inherited host
  search domain is not, or it would reopen the exfil channel. The exemption
  is the partner of the agent pod's `dnsConfig` search domains + `ndots:5`
  (§3): the search list turns a short `nats` into
  `nats.<ns>.svc.cluster.local`, which the exemption then routes to kube-dns.

Contract case T-NET-3 locks the behaviour: the agent resolves `nats` and the
gateway, while a non-allowlisted external domain NXDOMAINs.

### 6.4 gVisor

docker: `spec.runtime=runsc`; K8s: `runtimeClassName: gvisor`. Local Ubuntu
can install runsc so docker mode is fully verifiable; the K8s side is
validated on RKE2 (`k8s-prod-only` marker).

## 7. Storage model

### 7.1 Mount translation (same shape on both runtimes)

Containerizing the orchestrator introduces docker-out-of-docker path
translation: the orchestrator sees `/app/data/...`, but bind sources passed
to the host dockerd must be host paths. This is the same problem as K8s PVC
subPath translation:

```
business code:   VolumeMount(host_path=DATA_DIR / "spawns/<job>/skills")
                                 | relpath = path.relative_to(DATA_DIR)
DockerRuntime:   bind source = ROLEMESH_HOST_DATA_DIR / relpath
K8sRuntime:      volumeMounts: {name: data, subPath: relpath}
```

Business code (skill_projection etc.) is untouched; paths outside DATA_DIR
are denied by default on both runtimes, with exceptions declared in
deployment files (compose extra volume / values `agent.extraVolumes`);
`mount_security.py` still validates in-container target paths.

DooD semantics change (rev4): `additional_mounts` host paths (`~/projects`
and the like) still mount in docker mode — the bind source is interpreted by
the host dockerd — but the containerized orchestrator can no longer check
their existence, and a missing bind source makes dockerd silently create an
empty root-owned directory. Disposition: mount_security downgrades such paths
to pattern validation + prominent log warning, documented; on K8s they are
impossible — use `agent.extraVolumes`.

### 7.2 Volumes per environment

| Environment | Scheme |
|---|---|
| local docker | compose mounts `./data` into orchestrator; agents bind `ROLEMESH_HOST_DATA_DIR` (= `${PWD}/data`) |
| local kind | kind node extraMounts maps `./data`; hostPath PV or local-path PVC (single-node RWO suffices) |
| RKE2 production | RWX StorageClass (Longhorn RWX/NFS); without RWX, `storage.mode=rwo-colocated` (podAffinity) |

## 8. Known semantic differences (explicit in the contract doc)

| Difference | Handling |
|---|---|
| K8s merges stdout/stderr in pod logs | protocol output rides NATS; stderr is diagnostics-only -> acceptable; cases assert "diagnostic line appears" |
| pids limit is node-level | Helm NOTES + startup warning + RKE2 kubelet doc |
| emptyDir has no uid/gid options | image runs as UID 1000; default owner is the runtime user; T-FS locks it |
| Pod name conflict 409 | delete-then-create inside the runtime, matching docker |

## 9. Contract tests

```
tests/container/contract/
  conftest.py          # --runtime=docker|k8s; fixtures yield a real runtime and assert the deployment layer is up
  test_verify.py       # T-VER-*: verify_infrastructure fail-closed per missing invariant
  test_lifecycle.py    # T-LC-*:  run/exit code/stop/replace-on-name/orphan cleanup
  test_filesystem.py   # T-FS-*:  EROFS/tmpfs writable+ownership/mount translation ro|rw
  test_env_security.py # T-SEC-*: env allowlist/CapDrop/non-root/no SA token (k8s)
  test_network.py      # T-NET-*: no direct egress/bare-IP metadata unreachable/
                       #          DNS only via gateway/internal names (nats) resolve via gateway/
                       #          tokenless request 407/deny probe
  test_streams.py      # T-IO-*:  diagnostic stream visible/output cap
```

Preconditions: docker mode requires `docker compose up` infrastructure; k8s
mode requires the chart installed with `rolemesh-test` values. Tests do NOT
build infrastructure — that is itself a test of "declarative".

Acceptance: all green on local Ubuntu docker (incl. runsc); all green on kind
(Calico) on the same machine; one suite, zero runtime branches.

The positive K8s golden path (internal names resolve through the gateway
exemption AND the agent reaches the bus) has a second, in-cluster home: the
`connectivity-probe` helm test (paired with `deny-probe`). Unlike the pytest
contract suite — whose `verify_infrastructure` pre-flight hits the gateway
ClusterIP:3001 from the test process and so cannot run from OUTSIDE the
cluster (a host route to the Service CIDR is a non-portable hack, not a
fix) — the probe runs as a pod, reaches ClusterIPs natively, and gives a
repeatable regression guard on kind and RKE2 alike. It resolves
`nats`/`egress-gateway` and TCP-connects to nats:4222 + gateway:3001 (the
53-vs-1053 NetworkPolicy bug proved resolution ≠ reachability — connect,
don't just resolve), and confirms an external name stays blocked.

### Known limitations

- **Internal SRV/TXT stay refused.** The internal-name exemption runs after
  the A/AAAA/CNAME qtype gate, so SRV/TXT for internal names are still
  REFUSED. Sufficient today (NATS dials an A record); revisit only if an
  internal flow genuinely needs SRV.
- **The pytest contract suite's k8s mode needs cluster-network reach.** Its
  `verify_infrastructure` pre-flight assumes the runner can reach the
  gateway ClusterIP, true on an in-cluster/RKE2 node but not from a kind
  host. Until that pre-flight grows an in-cluster mode, the `helm test`
  connectivity-probe is the portable positive-path guard. (Tracked as a
  follow-up; not blocking.)

## 10. Deployment

### 10.1 Local: `deploy/compose/compose.yaml` (replaces process-style README)

Networks: `agent-net` (internal, `172.28.100.0/24` — uncommon range to avoid
collisions), `egress-net`. Services: nats, postgres, egress-gateway (fixed IP
`172.28.100.53` = `EGRESS_GATEWAY_DNS_IP`, depends only on NATS, healthz =
"NATS connected + default-deny ready", §5), orchestrator (depends_on healthy
nats/gateway, plus verify-retry in-app; mounts src/data/docker.sock), webui.

Starting point: `docker-compose.dev.yml` on main already declares dual-homed
NATS and owns agent-net — extend it, do not start from scratch.

README Quick Start becomes `container/build.sh && docker compose up`. No
hybrid profile (decided): the orchestrator no longer runs as a host process.

### 10.2 Production: Helm chart (`deploy/charts/rolemesh/`)

rev2 structure: orchestrator single-replica Deployment with
`strategy: Recreate` (rev4 — single stateful replica must not overlap during
rollout) + RBAC pods-only, webui, gateway Deployment+Service, 4
NetworkPolicies, PVC, Secrets (incl. `EGRESS_TOKEN_SECRET`), seed Job, NOTES.
Updates: gateway-policy allows NATS egress; gateway DNS via Service 53->1053;
namespace fully PSA restricted. NATS/Postgres are chart dependencies,
switchable to external instances.

### 10.3 Local kind

rootful Docker, Calico (kindnet has no NetworkPolicy), inotify pre-check,
single-arch amd64 `kind load`, `values-kind.yaml`.

## 11. Risks

| Risk | Mitigation |
|---|---|
| DooD path translation misconfigured | startup loopback self-check: write sentinel under data -> bind host path into a probe container -> unreadable = refuse to start |
| compose fixed subnet collides | uncommon range + documented override; verify checks network properties |
| kind CNI false-green | deny probe in `helm test` + contract T-NET (not at startup, §4.2) |
| RWX unavailable | `storage.mode=rwo-colocated` |
| pids limit node-level | NOTES + warning + kubelet doc |
| K8s watch drops | resourceVersion resume + periodic read + CONTAINER_TIMEOUT |
| Reverting #84 (EC=off) regret | fallback semantics move to the deployment layer: a separate compose file outside the default path; business code no longer forks |
| Dev-experience regression (in-container debugging) | bind-mount + reload; debugpy 5678; IDE config samples; **hard acceptance gate in S1-2** |

Decisions on record: single namespace to start; evaluation CLI docker-only;
rev3 declarative direction adopted; no hybrid profile; EC=off removed
(reverts #84); `remote_control.py` deleted (dead code, 2026-06-12).

## 12. Implementation plan

Three sessions, five steps; each step lands as an independently green commit.

- **S1-1 Infrastructure declarativization**: compose carries infra only
  (nats/postgres/gateway/networks/fixed IP); **gateway degraded startup
  lands here** (it is a prerequisite: the compose gateway starts before the
  host orchestrator's responders); startup logic provision -> verify; delete
  launcher/bootstrap/network-creation (+ eval CLI self-bootstrap); existing
  tests green; compose up + old host dev flow still works.
- **S1-2 Orchestrator containerization**: Dockerfiles (orchestrator, webui) +
  compose services + DooD translation + debugpy/reload; only now delete the
  host-topology code (CONTAINER_HOST_GATEWAY, loopback rewrite, ExtraHosts,
  EC=off, EGRESS_CONTROL_ENABLE, host credential proxy, remote_control);
  compute_egress_routing single-path; README. Gate: compose-up end-to-end
  agent session + dev inner loop (reload latency, debugpy breakpoints)
  acceptable.
- **S1-3 Contract test framework**: conftest + 6 case files, docker mode all
  green.
- **S2 (P2)**: k8s_runtime + verify-k8s + kind scripts; gate
  `--runtime=k8s` green on kind.
- **S3 (P3)**: Helm chart + RKE2 docs; gate `helm install` end-to-end on
  kind.

Steps 1<->2 are hard-ordered (host-gateway code cannot be deleted until the
orchestrator is in a container) and must not merge — merging destroys the
ability to tell "compose problem" from "containerization problem".
