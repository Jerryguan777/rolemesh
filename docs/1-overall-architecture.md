# Overall Architecture

This is the entry point to RoleMesh's architecture documentation. It covers the project background and goals, the end-to-end system diagram, what each major module does, the design choices behind those modules, the project's evolution from NanoClaw, and pointers into the per-module deep-dive docs.

If this is your first time reading the codebase, start here. Each module section ends with a link to its detailed design document.

---

## Background

Most agent platforms today fall into one of two camps:

- **Closed SaaS** (Claude Projects, Devin, ChatGPT Teams) — easy to use, but you don't own the data, you can't run on-prem, and the agent has no way to live inside your team's existing channels.
- **Single-tenant libraries** (LangChain, AutoGPT, CrewAI) — you own the code, but you have to build everything else yourself: tenant isolation, sandboxing, channel integration, credential management, audit, approval gates.

Neither shape fits when you want an AI coworker that handles **real company data**, **talks in your team's channels**, and **doesn't exfiltrate credentials**. RoleMesh exists for that gap: it is self-hosted, AGPL-licensed, multi-tenant from the database up, and sandboxed by architecture rather than by bolt-on filters.

The original code line started as [NanoClaw](https://github.com/qwibitai/nanoclaw), a single-user TypeScript Claude assistant. RoleMesh is the Python rewrite that grew it into a multi-tenant platform — see "Project lineage" below for the full step list.

---

## Goals

1. **Multi-tenant from the database up.** Tenant isolation enforced by Postgres Row-Level Security on every tenant-scoped table, with a dual-pool architecture (`rolemesh_app` `NOBYPASSRLS` + `rolemesh_system` `BYPASSRLS`) so the default posture is fail-closed. A buggy query hitting the app pool cannot accidentally leak across tenants.
2. **Sandboxed by architecture, not by bolt-on filters.** Three independent layers (container hardening + content safety pipeline + network egress chokepoint) — each layer assumes the others might fail.
3. **Two interchangeable agent runtimes.** Per-coworker choice between Claude SDK and Pi (the open-source, multi-provider runtime ported from pi-mono). Switch backends without rewriting tools, channels, or the orchestrator.
4. **Multiple human channels.** WebUI, Telegram, and Slack out of the box, behind a common channel-gateway protocol so adding a new one (Teams, Discord, …) is a localized change.
5. **Real human-approval flow.** Goes beyond chat: the agent can take real actions (refunds, price updates, access grants) but a policy can route any tool call into a human-approval gate before it executes.
6. **Per-coworker capability surface.** Each coworker gets its own MCP tools, skills, system prompt, and permission profile — so one tenant can have an "Operations Bot" that schedules tasks but cannot delegate to other agents, while another tenant has a "Manager Bot" that does both.

---

## Architecture diagram

![Overall Architecture](diagrams/Overall-Architecture.svg)

The diagram shows one tenant's worth of components. In a real deployment, the orchestrator hosts many tenants concurrently, each with its own agent containers, channel bindings, and DB row scope.

---

## Module responsibilities

### Orchestrator (`src/rolemesh/main.py`)

The central process. Owns the NATS connections, the Postgres pools, the channel gateways, the scheduler, the safety RPC server, the approval engine, and the agent-spawning loop. Every other module either runs inside the orchestrator process or is reached over NATS / HTTP from it.

The orchestrator is **stateless beyond its NATS + DB connections** — restarting it does not affect running agent containers (durable JetStream consumers replay missed messages on reconnect; orphan containers are cleaned up by name prefix `rolemesh-` on next boot).

### Agent containers (Claude SDK / Pi)

Each coworker turn runs in a short-lived Docker container. Two interchangeable backends share one image:

- **Claude SDK backend** (`src/agent_runner/claude_backend.py`) — wraps Anthropic's official `claude-agent-sdk`. First-class support for Claude Code workflows, MCP, skills, subagents, OAuth Max subscription token.
- **Pi backend** (`src/agent_runner/pi_backend.py`) — wraps the in-tree Pi runtime (`src/pi/`). Multi-provider (Anthropic, OpenAI, Gemini, Bedrock); cleaner streaming events; fork-friendly session model.

The backend choice is per-coworker (`coworkers.agent_backend` column) or per-process (`ROLEMESH_AGENT_BACKEND` env var). The orchestrator and the channel gateways do not know — and do not need to know — which backend is inside any given container.

→ `docs/agent-executor-and-container-runtime.md`, `docs/switchable-agent-backend.md`, `docs/backend-stop-contract.md`

### NATS bus

All Orchestrator ↔ Agent IPC, plus channel ↔ orchestrator IPC, plus several internal RPCs, ride a single NATS server (with JetStream + KV). The agent_runner has no direct connection to the orchestrator — it reads its initial config from a KV bucket, publishes results / messages / task ops to JetStream subjects, and receives follow-ups / stop / shutdown signals over the same bus.

The NATS choice replaced the original NanoClaw IPC (stdin pipe + stdout markers + file polling) — see "Why these choices" below.

→ `docs/nats-ipc-architecture.md`

### Channel gateways (Telegram, Slack, WebUI)

A `ChannelGateway` protocol abstracts how a chat platform delivers user messages and receives agent replies. The Telegram and Slack gateways run inside the orchestrator process (event-driven listeners). The WebUI is a separate FastAPI process that talks to the orchestrator over the `web-ipc` NATS namespace — keeping HTTP concerns out of the orchestrator and letting the WebUI scale independently.

→ `docs/webui-architecture.md`

### MCP tools (in-process + external)

Every agent has two kinds of tools:

- **In-process MCP tools** — the `rolemesh` MCP server, exposing `send_message`, `schedule_task`, `pause_task`, `list_tasks`, etc. These are direct Python function calls inside the agent container, with NATS as the wire format back to the orchestrator.
- **External MCP servers** — operator-configured MCP endpoints (CRM, ERP, internal APIs). The agent container never sees the auth token: it talks to a local credential proxy that rewrites the `Authorization` header at the HTTP layer using a token vault on the host. JWTs are refreshed via OIDC-style flows on the host side.

→ `docs/external-mcp-architecture.md`

### Hooks system

A unified `HookHandler` protocol bridges Claude SDK's hooks (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `PreCompact`, `Stop`) and Pi's extension events (`tool_call`, `tool_result`, `session_before_compact`). Audit, DLP, transcript-archive, approval, and observability handlers are all written once against the unified protocol — they fire on whichever backend the coworker happens to use.

→ `docs/hooks-architecture.md`

### Approval module

Policy-driven human-in-the-loop gate for high-risk MCP tool calls. The container-side hook intercepts tool calls that match a policy, suspends them, and waits for an `approval.decided.{id}` event published by either the WebUI's REST decide endpoint or by an automatic approver. Designed so a deployment with **no policies** is bit-identical to a build without the approval module — zero overhead when nobody configured it.

→ `docs/approval-architecture.md`

### Safety framework

Three-stage content pipeline (`INPUT_PROMPT`, `PRE_TOOL_CALL`, `MODEL_OUTPUT`, plus `POST_TOOL_RESULT` and `PRE_COMPACTION`) with five verdict actions (`allow` / `block` / `redact` / `warn` / `require_approval`). Eight built-in checks split into a **cheap** in-container set (`pii.regex`, `domain_allowlist`, `secret_scanner`) and a **slow** orchestrator-RPC set (`presidio.pii`, `llm_guard.prompt_injection`, `llm_guard.jailbreak`, `llm_guard.toxicity`, `openai_moderation`). The slow set is gated on the `[safety-ml]` extra so cheap-only deployments stay light.

→ `docs/safety/safety-framework.md`

### Container hardening

Every agent container ships with `CapDrop=ALL`, no-new-privileges, AppArmor `docker-default`, `ReadonlyRootfs=true` plus tmpfs carve-outs, user-namespace remap (deployment-time), per-container resource ceilings (memory / CPU / PIDs / fd / swap), env-allowlist (the orchestrator never forwards arbitrary env vars), Docker socket bind blockade, and an OCI runtime switch (`runc` / `runsc`).

→ `docs/safety/container-hardening.md`

### Egress gateway (network layer)

Agent containers run on a Docker bridge with `Internal=true` — they have no route to the internet. A dual-homed `egress-gateway` container straddles the agent bridge and the egress bridge; every outbound flow (LLM API, external MCP, package download, …) goes through it. The gateway runs an HTTP CONNECT forward proxy (port 3128), an authoritative DNS resolver (port 53, per-tenant allowlists), and a credential-injecting reverse proxy (port 3001, where the actual API tokens live).

This is the third independent safety layer: even if a malicious agent escapes the content pipeline AND escapes the container, it still has no path to `curl example.com`.

→ `docs/egress/deployment.md`

### Auth (AuthN + AuthZ)

Authentication is delegated to a pluggable `AuthProvider` (External JWT, Builtin, OIDC). Authorization is always RoleMesh's own logic — `AgentPermissions` (4 fields: `data_scope`, `task_schedule`, `task_manage_others`, `agent_delegate`) controls what an agent can do; user roles (owner / admin / member) control what humans can do. Auth checks happen at four interception points (IPC handler, REST middleware, channel inbound, container spawn) and **nowhere else**.

→ `docs/auth-architecture.md`

### Skills

Per-coworker capability folders: a `SKILL.md` (markdown + YAML frontmatter) plus optional supporting files (reference docs, examples, scripts). Stored in Postgres with RLS, projected per-spawn into a read-only bind mount, never shared across tenants. The model auto-invokes a skill based on its frontmatter `description` — no slash commands, no human-side wiring. Backend-aware frontmatter lets one skill body serve both Claude SDK (`/home/agent/.claude/skills`) and Pi (`/home/agent/.pi/skills`); fields scoped to the other backend are dropped at projection time.

→ `docs/skills-architecture.md`

### Event stream + Steering

The WebUI shows real-time progress events (`container_starting`, `running`, `tool_use`) so users aren't staring at a silent spinner during long turns. The Stop button publishes an `agent.{job}.interrupt` over JetStream that aborts the current turn without killing the container, so the user can immediately redirect with a follow-up message — a 30-second cold-start is paid once per coworker session, not once per Stop.

→ `docs/event-stream-architecture.md`, `docs/steering-architecture.md`

### Evaluation framework

`rolemesh-eval` CLI (Inspect AI based) measures how coworker behavior changes across `system_prompt` / `tools` / `skills` / `agent_backend` / `model` configurations. Reuses the production `ContainerAgentExecutor` so eval runs the same code path that handles real traffic — no parallel orchestrator that drifts away from prod. Each run snapshots the coworker's full config with a sha256, so `rolemesh-eval list` clusters runs that share a configuration.

(No standalone doc yet — see the README "Evaluation" section and `src/rolemesh/evaluation/`.)

### Database (Postgres + RLS)

Postgres 16 with Row-Level Security on every tenant-scoped table. Two pool architecture:

- `rolemesh_app` — `NOBYPASSRLS`, used by all business-logic queries. RLS-enforced; a `SET LOCAL rolemesh.tenant_id` GUC scopes every query.
- `rolemesh_system` — `BYPASSRLS`, used only by schema migrations, system-wide cleanup, and the safety / approval RPC paths that legitimately need cross-tenant reads. Calls are explicit (`tenant_conn` vs `admin_conn`), so the difference is visible at every call site.

Schema lives in `src/rolemesh/db/schema.py`; per-entity CRUD is split into `db/{tenant,user,coworker,chat,task,skill,approval,safety}.py`.

→ `docs/multi-tenant-architecture.md`

### Scheduler (Cron)

Cron-style task scheduler inside the orchestrator (croniter). Triggers spawn an agent container the same way a human message would, but flags `is_scheduled_task=true` in the init payload so the agent prompt is wrapped with `[SCHEDULED TASK]` framing. Tasks are stored in `scheduled_tasks` with RLS — agents can only see / manage their own tenant's tasks (further filtered by `data_scope`).

---

## Why these choices

Six load-bearing decisions that shape everything else:

### 1. Per-coworker, short-lived containers (instead of long-running workers)

A single coworker handles many concurrent turns and many users. The naive design is a long-running worker process per coworker; the chosen design is a fresh container per turn (or per session). The cost is cold-start latency (~3–10 s); the win is that:
- Fault isolation is automatic — a poisoned session can't leak into the next one.
- Container hardening is real — the rootfs is ephemeral, so write-anywhere exploits don't persist.
- Resource ceilings are hard — the kernel kills the cgroup, no need for in-process limits.
- Backend swaps are atomic — change `coworkers.agent_backend` and the next spawn picks the new one up; no rolling restart of long-running workers.

The 3–10 s cold start is mitigated by the steering design (Stop interrupts the current turn but keeps the container alive for follow-ups within the session).

### 2. NATS as the universal IPC

The original NanoClaw used three IPC mechanisms in one codebase: stdin pipe (initial input), stdout markers (`---NANOCLAW_OUTPUT_START---`), and file polling (everything else). All three coupled the orchestrator and agent to the same host — they would not scale to a Kubernetes deployment.

NATS (with JetStream + KV) replaces all three with one system, and adds:
- Cross-host scheduling (the agent container can run on a different node).
- Durable consumers, so an orchestrator restart replays missed messages instead of dropping them.
- A clean wire-format shape — JSON over named subjects, easy to inspect with the NATS CLI.

The same NATS server also carries WebUI ↔ orchestrator (`web-ipc`), approval signals (`approval-ipc`), and several internal RPCs (`egress.*`, `safety.*`, `orchestrator.agent.lifecycle`).

→ `docs/nats-ipc-architecture.md`

### 3. Internal-only agent network + dual-homed egress gateway

After Container Hardening shipped, agents still had unrestricted outbound traffic — `curl evil.com` would just work. The fix is structural rather than filter-based: the agent bridge is `Internal=true` (Docker prevents any direct internet route), and a single dual-homed gateway container is the only path out. The gateway enforces per-tenant DNS allowlists and reverse-proxies LLM / MCP traffic with credentials injected at the host boundary.

This is the third independent safety layer — orthogonal to container hardening (which stops sandbox escape) and the safety framework (which stops bad prompts / outputs). Any one layer can fail; all three failing simultaneously is the threat model.

→ `docs/egress/deployment.md`, `docs/safety/toggle-experiments.md`

### 4. Two interchangeable agent backends

Locking into one LLM framework was unacceptable: vendor pricing, rate limits, and feature roadmaps all become single points of failure. The `AgentBackend` protocol abstracts the SDK so the rest of the system (orchestrator, channels, NATS protocol, MCP tools, approval gate) is backend-agnostic.

The two backends differ in mechanics but not in observable behavior — Claude SDK uses preemptive cancellation (`Task.cancel()`), Pi uses cooperative cancellation (`asyncio.Event`); the **Stop contract** (`docs/backend-stop-contract.md`) documents the four observable behaviors any backend must deliver, regardless of how it implements them internally.

→ `docs/switchable-agent-backend.md`

### 5. Database-level multi-tenancy via Postgres RLS

Two tenants on the same orchestrator must never see each other's data, even if a query is buggy. The chosen primitive is Postgres Row-Level Security at the database role level — not application-level filters that a forgotten `WHERE tenant_id=` would defeat.

The dual-pool design (`rolemesh_app` `NOBYPASSRLS` + `rolemesh_system` `BYPASSRLS`) makes the trust boundary explicit: business code uses `tenant_conn(...)` which routes to the app pool with `SET LOCAL rolemesh.tenant_id`, and system code uses `admin_conn(...)` which routes to the system pool. A code review can see at every call site which side of the boundary a query is on.

→ `docs/multi-tenant-architecture.md`

### 6. Three orthogonal safety layers (defense in depth)

Each layer is designed assuming the others have failed:

| Layer | Stops | If breached, the next layer catches |
|---|---|---|
| **Content pipeline** (`safety/safety-framework.md`) | Malicious prompts, PII leaks in outputs, jailbreaks | Prompt injection that bypasses the pipeline still cannot run a privileged container syscall |
| **Container hardening** (`safety/container-hardening.md`) | Sandbox escape, host filesystem access, capability abuse | A compromised agent that escapes the container still has no internet route |
| **Network egress** (`egress/deployment.md`) | Data exfiltration, C2 callback, credential theft via DNS | The gateway's per-tenant allowlist + credential proxy means tokens never reach the agent process |

Plus **human-approval flow** (`approval-architecture.md`) as an orthogonal "judgment" layer — for cases where the agent has every legitimate permission but the operator wants a human to look at this specific action before it runs.

→ `docs/safety/attack-simulation-matrix.md` tracks every modeled attack against these three layers with the corresponding test.

---

## Project lineage

RoleMesh evolved from [NanoClaw](https://github.com/qwibitai/nanoclaw), a single-user TypeScript Claude assistant. The work split into two phases:

### Phase 1 — Inside NanoClaw (steps 1–6)

Done in-place on the NanoClaw codebase, eventually growing it past its single-user origins:

1. **TypeScript → Python rewrite.** A clean port that kept NanoClaw's surface but moved to the Python ecosystem (`asyncio`, `aiodocker`, `asyncpg`).
2. **File-based IPC → NATS-based IPC.** Replaced stdin-pipe + stdout-markers + file-polling with a single NATS bus (KV + JetStream + request-reply). See `docs/nats-ipc-architecture.md`.
3. **Agent executor + container runtime abstraction.** Untangled the original 340-line `run_container_agent()` into two independent layers: `ContainerRuntime` (how to start a container) and `AgentExecutor` (what to do with it). See `docs/agent-executor-and-container-runtime.md`.
4. **SQLite → Postgres.** Dropped the single-file DB for proper concurrent access, schema migrations, and (later) row-level security.
5. **Slack channel.** Added Slack alongside the existing Telegram channel via a common `ChannelGateway` protocol.
6. **Multi-tenant.** Tenant + Coworker + ChannelBinding + Conversation entity model. This was the change that broke "single-user assistant" decisively — the codebase was now a platform.

### Phase 2 — RoleMesh fork (steps 7+)

After step 6, the codebase was forked from NanoClaw and renamed to RoleMesh (project name + every code identifier). Subsequent work happened on the new repo:

7. **WebUI.** FastAPI + WebSocket + Lit frontend, running as a separate process to keep HTTP concerns out of the orchestrator. See `docs/webui-architecture.md`.
8. **AuthN + AuthZ.** Pluggable auth providers (External JWT / Builtin / OIDC), four-field `AgentPermissions` model, OIDC PKCE login. See `docs/auth-architecture.md`.
9. **External MCP tools.** Credential proxy + token vault + token refresh, so the agent container never sees real auth tokens. See `docs/external-mcp-architecture.md`.
10. **Switchable agent backend.** The Pi backend integration — second runtime alongside Claude SDK, controlled by `coworkers.agent_backend`. See `docs/switchable-agent-backend.md`.
11. **Hooks.** Unified hook system across Claude SDK and Pi. See `docs/hooks-architecture.md`.
12. **Event stream.** Real-time progress events to the WebUI. See `docs/event-stream-architecture.md`.
13. **Steering.** Stop button + follow-up-while-running (true mid-turn steering deferred). See `docs/steering-architecture.md` and `docs/backend-stop-contract.md`.
14. **Approval.** Policy-gated human-in-the-loop for high-risk MCP calls. See `docs/approval-architecture.md`.
15. **Safety stack.** Three layers — container hardening, content safety framework, network egress control. See `docs/safety/container-hardening.md`, `docs/safety/safety-framework.md`, `docs/egress/deployment.md`.
16. **RLS.** Postgres Row-Level Security on every tenant-scoped table; dual-pool architecture. See `docs/multi-tenant-architecture.md`.
17. **Skills.** Per-coworker markdown skill folders, projected per-spawn. See `docs/skills-architecture.md`.
18. **Evaluation.** `rolemesh-eval` CLI based on Inspect AI; reuses the production `ContainerAgentExecutor`.
19. **Observability.** OpenTelemetry tracer + W3C trace-context propagation across NATS subjects (in progress).

The split matters when reading the older code or older docs: anything dated before phase 2 may still talk about NanoClaw, and the IPC + container abstraction designs (`nats-ipc-architecture.md`, `agent-executor-and-container-runtime.md`) describe phase-1 work that pre-dates the rename.

---

## Per-module documentation

Grouped by topic. Every doc focuses on the *why* — the alternatives considered and the trade-offs taken — so you can extend the module without re-litigating the original decisions.

### Containers and agent runtime

- [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md) — `ContainerRuntime` + `AgentExecutor` two-layer split
- [`switchable-agent-backend.md`](switchable-agent-backend.md) — Per-coworker Claude SDK / Pi selection
- [`backend-stop-contract.md`](backend-stop-contract.md) — Observable behaviors any backend must deliver on Stop

### IPC

- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) — Six-channel NATS protocol; KV + JetStream + request-reply

### Multi-tenancy and identity

- [`multi-tenant-architecture.md`](multi-tenant-architecture.md) — Tenant / Coworker / Conversation entity model, Postgres RLS
- [`auth-architecture.md`](auth-architecture.md) — `AgentPermissions`, four interception points, three deployment modes

### Channels

- [`webui-architecture.md`](webui-architecture.md) — FastAPI + Lit, separate-process design

### Agent behavior

- [`hooks-architecture.md`](hooks-architecture.md) — Unified hook system bridging Claude SDK and Pi
- [`event-stream-architecture.md`](event-stream-architecture.md) — Real-time progress events to the WebUI
- [`steering-architecture.md`](steering-architecture.md) — Stop button + follow-up-while-running
- [`skills-architecture.md`](skills-architecture.md) — Per-coworker skill folders, per-spawn projection

### Tools and human-in-the-loop

- [`external-mcp-architecture.md`](external-mcp-architecture.md) — Credential proxy + token vault for external MCP servers
- [`approval-architecture.md`](approval-architecture.md) — Policy-gated approval flow

### Safety

- [`safety/safety-framework.md`](safety/safety-framework.md) — Three-stage content pipeline, eight checks, cheap / slow split
- [`safety/container-hardening.md`](safety/container-hardening.md) — CapDrop / readonly rootfs / userns / runsc / etc.
- [`safety/attack-simulation-matrix.md`](safety/attack-simulation-matrix.md) — Modeled attacks vs. defending layer
- [`safety/toggle-experiments.md`](safety/toggle-experiments.md) — Empirical A/B comparison of the three safety layers
- [`egress/deployment.md`](egress/deployment.md) — `Internal=true` agent bridge + dual-homed gateway operator guide

A Chinese translation of every doc lives next to it as `*-cn.md`.
