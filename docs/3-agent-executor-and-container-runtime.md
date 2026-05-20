# Container Runtime & Agent Executor Architecture

This document describes the two abstraction layers that sit between the Orchestrator and the actual container processes: **ContainerRuntime** (how containers are started and managed) and **AgentExecutor** (how agent work is dispatched). It covers the problem these abstractions solve, the design decisions behind them, and how they enable swapping the container backend (Docker → Kubernetes) and the agent backend (Claude SDK → Pi) independently.

> **Project lineage.** RoleMesh evolved from [NanoClaw](https://github.com/qwibitai/nanoclaw); this two-layer split was untangled from a single 340-line function during the rewrite, so the historical sections below talk about the original NanoClaw shape being replaced. The abstraction itself is RoleMesh-era; later phases (multi-tenant, container hardening, egress control) extended `ContainerSpec` with additional fields without changing the layer split.

---

## The Problem: Two Things Tangled Together

The original NanoClaw had a single ~340-line function, `run_container_agent()`, that did everything in one call:

1. Build volume mount lists
2. Construct `docker run` CLI arguments as string arrays
3. Call `asyncio.create_subprocess_exec("docker", "run", ...)`
4. Write initial input to NATS KV
5. Subscribe to NATS JetStream for streaming results
6. Read stderr from the subprocess pipe
7. Manage activity-based timeout
8. Write execution logs to disk
9. Return the final result

This mixed two unrelated concerns:

- **Container lifecycle** (steps 1–3): how to start, stop, and monitor a container — the mechanics of Docker commands, volume mounts, environment variables, process management.
- **Agent orchestration** (steps 4–9): what to do with the container once it is running — write the prompt, subscribe to results, handle timeouts, collect output.

These need to change independently:

- Switching from Docker to Kubernetes changes the container lifecycle but not the agent orchestration.
- Switching from Claude SDK to Pi changes the agent inside the container but not how the container itself is managed.

A single function couldn't be evolved along either axis without risk to the other.

---

## The Solution: Two Layers

```
                        Orchestrator (main.py)
                              │
                    ┌─────────▼──────────┐
                    │   AgentExecutor     │  "What work to do"
                    │   Protocol          │
                    ├─────────────────────┤
                    │ ContainerAgent-     │  Writes KV, subscribes NATS,
                    │   Executor          │  manages timeout, collects output
                    └─────────┬──────────┘
                              │
                    ┌─────────▼──────────┐
                    │  ContainerRuntime   │  "How to run containers"
                    │  Protocol           │
                    ├─────────────────────┤
                    │  DockerRuntime      │  Docker Engine API (aiodocker)
                    │  (K8sRuntime)       │  Kubernetes Jobs (future)
                    └─────────┬──────────┘
                              │
                         Container
                    (Claude SDK or Pi)
```

The two layers communicate through small data types (`ContainerSpec`, `ContainerHandle`, `AgentInput`, `AgentOutput`) that contain no behavior — purely shapes. Code on either side of the boundary can be swapped, mocked, or inspected without touching the other.

---

## Layer 1: ContainerRuntime

The low-level layer. It knows how to check that the container backend is available, start a container from a specification, stop a running container, and clean up orphans from a previous crash. It does **not** know about agents, prompts, NATS, sessions, or any business logic.

The protocol exposes five methods: `ensure_available`, `run`, `stop`, `cleanup_orphans`, `close`. Anything that satisfies that shape (Python `Protocol` — see "Design Trade-offs" below) is a valid runtime.

### ContainerSpec: what to run

A frozen dataclass describing everything needed to start a container. The minimal core is what you would expect — `name`, `image`, `mounts`, `env`, `user`, `memory_limit`, `cpu_limit`, `entrypoint`, `extra_hosts`. On top of that, two field groups were added by later phases:

- **Hardening fields** — `cap_drop`, `security_opt`, `readonly_rootfs`, `tmpfs`, `pids_limit`, `memory_swap`, `memory_swappiness`, `ulimits`, `runtime` (`runc` / `runsc`). All default to safe values; existing call sites that only set name/image/env still compile. Rationale lives in [`safety/container-hardening.md`](safety/container-hardening.md).
- **Network fields** — `network_name`, `dns`. EC-1 attaches every agent to an `Internal=true` bridge with the egress gateway as the authoritative DNS. Topology lives in [`egress/deployment.md`](egress/deployment.md).

The contract here is just "the spec carries everything the runtime needs"; the *content* of those fields is owned by the security/network docs.

The spec is built by pure functions in `container/runner.py` (`build_volume_mounts`, `build_container_spec`) — they compute the spec from a coworker's configuration without any I/O. Keeping them pure means a dry-run "show me the spec" mode, or a unit test, can call them without mocking Docker.

### ContainerHandle: what you get back

A handle to a running container. Intentionally minimal — three methods: `wait()` for exit, `stop(timeout)` to terminate, and `read_stderr()` for log streaming. **No `read_stdout_line()` or `write_stdin()`.** Three reasons:

1. **NATS replaced stdin/stdout.** The agent reads its initial input from NATS KV and publishes results to JetStream. There is no pipe communication needed. See [`nats-ipc-architecture.md`](nats-ipc-architecture.md).
2. **Docker API stdin/stdout is complex.** It requires WebSocket attach with custom buffering and framing — getting it reliable across Docker and Kubernetes is harder than it looks.
3. **Simpler handle = easier K8s port.** Kubernetes Jobs don't have a stdin pipe at all.

The one remaining I/O method, `read_stderr()`, is plain log streaming — no framing, no bidirectionality, just a byte stream for diagnostics.

### DockerRuntime: the current implementation

Uses `aiodocker` (async Docker Engine API client) instead of subprocess calls.

| Approach | Why we didn't pick it |
|---|---|
| `subprocess.run(["docker", "run", ...])` | String-based argument construction is fragile, no structured error handling, can't stream stderr cleanly. |
| `asyncio.create_subprocess_exec(...)` | Better, but still string args; manual process management; doesn't translate to Kubernetes. |
| **`aiodocker` (Docker Engine API)** | Structured config dicts, native async, proper error types, similar API shape as the Kubernetes client. |

`aiodocker` gives us exactly what we need with async support, in roughly 200 lines of `DockerRuntime`.

**Quirk worth knowing**: Docker's `AutoRemove` flag (the API equivalent of `docker run --rm`) races with `container.wait()` — by the time you read the exit code the container may already be gone. We skip `AutoRemove` and delete explicitly in `ContainerHandle.stop()`.

### K8sRuntime: a future extension point

`ContainerSpec` maps cleanly to a Kubernetes Job (`name → metadata.name`, `image → spec.containers[0].image`, `mounts → volumes + volumeMounts`, etc.). A `K8sRuntime` would use `kubernetes-asyncio` to create Jobs, watch for completion, and stream logs; the `ContainerAgentExecutor` above it wouldn't change at all. Today this is a stub that raises `NotImplementedError` — the abstraction earns its keep mainly as a clean swap point when Kubernetes deployment is needed.

### Runtime selection

A factory reads `CONTAINER_RUNTIME` (`docker` default, `k8s` future) and returns the matching runtime instance. The Orchestrator calls it once at startup and passes the instance to everything that needs it.

---

## Layer 2: AgentExecutor

The high-level layer. It knows how to write the agent's initial input to NATS KV, start a container (via ContainerRuntime), subscribe to JetStream for streaming results, manage activity-based timeout, read and log stderr, and return structured output. It does **not** know how containers are started or stopped — that is ContainerRuntime's job.

The protocol takes an `AgentInput` and returns an `AgentOutput`, plus two callbacks: `on_process(container_name, job_id)` so the scheduler can track active containers, and `on_output(parsed)` so the orchestrator can stream each result block back to the user as it arrives.

### Why a single implementation, not per-backend executor classes

When we evaluated the Pi backend we found the **orchestrator-side** flow is identical for every agent backend:

1. Build volume mounts
2. Build container spec
3. Write initial input to NATS KV
4. Start container
5. Subscribe to NATS results
6. Manage timeout
7. Read stderr
8. Return output

The only differences are configuration: container image, entrypoint, a few extra mounts, a few extra env vars. So instead of:

```
❌  ClaudeCodeExecutor   (orchestration logic)
❌  PiExecutor           (same orchestration logic, different config)
```

we have:

```
✅  ContainerAgentExecutor + AgentBackendConfig
```

— one class, configured per backend by a small frozen dataclass. Adding a third backend is a new `AgentBackendConfig` constant, not a new class.

### AgentBackendConfig and the single-image design

`AgentBackendConfig` carries `name`, `image`, `entrypoint`, `extra_mounts`, `extra_env`, `skip_claude_session`. Two presets exist today:

- `CLAUDE_CODE_BACKEND` (`name="claude"`)
- `PI_BACKEND` (`name="pi"`)

Both presets reference the **same Docker image** (`rolemesh-agent:latest`); the agent_runner inside the image picks the runtime path at startup based on the `AGENT_BACKEND` env var, which the executor injects via `extra_env`.

**Why single image instead of one image per backend?**

- Smaller image cache, simpler build pipeline, single place to apply hardening.
- Backend swap at runtime is just an env var — no image pull, no rolling deploy.
- Coworkers that switch backend mid-life get the new behavior on the next spawn without any container build coordination.

The cost is a slightly larger image (Pi dependencies are pulled even when the coworker uses Claude SDK). For a self-hosted platform image that's a few hundred MB once, not per spawn — the trade-off pays off.

### Backend selection: per-coworker dispatch

The orchestrator does **not** pick a backend at the process level. At startup it builds one `ContainerAgentExecutor` per `AgentBackendConfig` and stores them in a dict:

```
_executors = {
    "claude": ContainerAgentExecutor(CLAUDE_CODE_BACKEND, ...),
    "pi":     ContainerAgentExecutor(PI_BACKEND, ...),
}
```

When a turn arrives, the orchestrator looks up the executor by `coworker.agent_backend`. **Different coworkers in the same orchestrator run on different backends concurrently** — common in multi-tenant scenarios where one tenant prefers Claude SDK and another routes through Pi+Bedrock for compliance.

A global default (`ROLEMESH_AGENT_BACKEND` env var) only matters for coworkers whose row has a NULL `agent_backend` — in practice an empty escape hatch.

---

## How the Two Layers Work Together

A complete agent invocation:

```
1. Orchestrator receives a message for a coworker
        │
2. Pick executor by coworker.agent_backend → ContainerAgentExecutor
        │
3. ContainerAgentExecutor.execute(AgentInput(...))
        │
        ├── build_volume_mounts(coworker, permissions, backend_config)
        │     → list[VolumeMount]
        │
        ├── build_container_spec(mounts, name, job_id, backend_config)
        │     → ContainerSpec   (with hardening + network fields filled in)
        │
        ├── Write AgentInitData to NATS KV "agent-init.{job_id}"
        │     (carries permissions, mcp_servers, safety_rules, approval_policies, …)
        │
        ├── runtime.run(spec)                    ← ContainerRuntime layer
        │     → ContainerHandle
        │
        ├── on_process(container_name, job_id)   ← scheduler tracks this
        │
        ├── Subscribe to agent.{job_id}.results  ← NATS JetStream
        │
        ├── Start timeout watcher + stderr reader tasks
        │
        ├── (Inside the container, hooks/safety/approval/skills run as the
        │    LLM produces tool calls and outputs — orchestrator side just
        │    consumes events from NATS)
        │
        ├── Wait for container exit              ← handle.wait()
        │
        ├── Cancel subscriptions and tasks
        │
        └── Return AgentOutput(status, result, new_session_id)
```

The cut is clean: anything calling `runtime.*` or `handle.*` is the ContainerRuntime layer. Everything else (NATS, timeout, logging, output parsing) is the AgentExecutor layer.

What runs *inside* the container — hook handlers, safety pipeline, approval gating, skill loading, MCP tool dispatch — is each documented in its own file ([`hooks-architecture.md`](hooks-architecture.md), [`safety/safety-framework.md`](safety/safety-framework.md), [`approval-architecture.md`](approval-architecture.md), [`skills-architecture.md`](skills-architecture.md), [`external-mcp-architecture.md`](external-mcp-architecture.md)). From the executor's perspective they are just events on NATS.

---

## Concurrency: GroupQueue

`ContainerAgentExecutor` is the *invocation* primitive — it spawns one container per call. The decision of *when* to spawn is delegated to `GroupQueue` (in `container/scheduler.py`), which enforces three independent concurrency ceilings:

- **Global** — total agent containers across the orchestrator.
- **Per-tenant** — one tenant cannot exhaust the global quota.
- **Per-coworker** — one chatty coworker cannot exhaust its tenant's quota.

When a turn arrives, `GroupQueue` either dispatches it to the executor immediately or queues it until the appropriate ceiling has free capacity. The runtime layer is unaware of this — it sees a steady drip of `runtime.run(spec)` calls.

---

## Platform Helpers

Two small platform-specific concerns live as module-level functions in `runtime.py`, independent of any runtime implementation:

- **Proxy bind host** — the credential proxy needs to bind to an address the container can reach. On macOS / WSL it's `127.0.0.1` (Docker Desktop routes `host.docker.internal` to host loopback); on native Linux it's the `docker0` bridge IP (typically `172.17.0.1`); fallback is `0.0.0.0`. Detection uses `fcntl.ioctl` with `SIOCGIFADDR`.
- **Host gateway** — on Linux, `host.docker.internal` doesn't resolve by default; an `--add-host` entry is injected into every spec.

After EC-1 most agent traffic no longer reaches the host loopback at all — agents are on an `Internal=true` bridge and outbound flows go through the egress gateway. These helpers still serve the legacy / debug code paths and the gateway container itself; the production path is described in [`egress/deployment.md`](egress/deployment.md).

---

## Design Trade-offs

### Why `Protocol`, not `ABC`?

Python's `typing.Protocol` enables structural subtyping — a class satisfies the protocol if it has the right methods, without inheriting from it. This means:

- `DockerRuntime` doesn't need `class DockerRuntime(ContainerRuntime)`; it just implements the methods.
- Tests can use simple mock objects without inheritance.
- The runtime module doesn't need to import the implementation modules.

ABCs would force inheritance, import dependencies, and registration boilerplate for no practical benefit.

### Why not a higher-level orchestration library?

Libraries like `docker-py` (synchronous) or full orchestration frameworks (Kubernetes Operator SDK) add complexity we don't need. Our requirements are simple — start a container with some config, wait for exit, read stderr, stop, clean up orphans. `aiodocker` covers this with async support in ~200 lines.

### Why separate `build_volume_mounts` / `build_container_spec` from the executor?

These are pure functions: given inputs they produce a `ContainerSpec` with no side effects. Keeping them outside the executor class means:

- They're testable without mocking Docker or NATS.
- They can be reused (dry-run mode, container spec preview, eval framework).
- The executor class focuses on orchestration flow, not configuration computation.

### Why no stdin/stdout on `ContainerHandle`?

Covered above ("Layer 1 → ContainerHandle"). Short version: NATS replaced stdin/stdout, Docker API stdin/stdout is complex, simpler handle ports cleanly to Kubernetes.

---

## Container Naming and Orphan Cleanup

Container names follow the pattern `rolemesh-{safe_group_folder}-{epoch_ms}` (e.g. `rolemesh-main-1711612800000`). On startup the Orchestrator calls `runtime.cleanup_orphans("rolemesh-")` to find and remove containers left over from a previous crash. The prefix-based filter catches every RoleMesh container regardless of which coworker or job created it.

---

## Dependency Graph

```
main.py
  │
  ├── get_runtime() → DockerRuntime
  │
  ├── For each AgentBackendConfig (CLAUDE_CODE_BACKEND, PI_BACKEND):
  │       _executors[name] = ContainerAgentExecutor(cfg, runtime, transport, get_coworker)
  │
  └── GroupQueue(transport, runtime, orchestrator_state)
        │
        ├── Dispatches to _executors[coworker.agent_backend].execute(...)
        ├── runtime.stop(name)            ← for shutdown
        └── transport.nc.request("agent.{job_id}.shutdown", ...) ← graceful close
```

`ContainerRuntime` is injected into both the executor (to start containers) and the scheduler (to stop them at shutdown). Neither side depends on a specific implementation — both program against the Protocol.

---

## Related documentation

- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) — IPC protocol the executor uses
- [`switchable-agent-backend.md`](switchable-agent-backend.md) — agent-side `AgentBackend` protocol (the runtime *inside* the container)
- [`backend-stop-contract.md`](backend-stop-contract.md) — observable behaviors any backend must deliver on Stop
- [`safety/container-hardening.md`](safety/container-hardening.md) — what fills in the hardening fields of `ContainerSpec`
- [`egress/deployment.md`](egress/deployment.md) — what fills in the network fields of `ContainerSpec`
