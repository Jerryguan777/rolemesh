# NATS-Based IPC Architecture

This document describes how RoleMesh's Orchestrator and container Agents communicate using NATS. It covers the problem with the original approach, why NATS was chosen, the 6-channel protocol design, and the NATS primitives used for each channel.

> **Project lineage.** RoleMesh started as a Python rewrite of [NanoClaw](https://github.com/qwibitai/nanoclaw); the move from file-based IPC to NATS happened during that rewrite, so the historical sections below talk about the original NanoClaw approach being replaced. Subjects added later (`interrupt`, `safety_events`, plus the `web-ipc` / `approval-ipc` streams) are RoleMesh-era additions on top of the same NATS bus.

## Background: Why Not Files or stdin/stdout?

The original NanoClaw used three separate mechanisms for Orchestrator-Agent communication:

1. **stdin** — Orchestrator piped initial JSON to the container's standard input
2. **stdout markers** — Agent wrote results between `---NANOCLAW_OUTPUT_START---` / `---NANOCLAW_OUTPUT_END---` markers on standard output
3. **File-based IPC** — Agent wrote JSON files to shared directories, Orchestrator polled them every second

This worked for a single-user tool, but had fundamental problems:

| Mechanism | Problem |
|-----------|---------|
| stdin JSON | Kubernetes Jobs don't support stdin piping. Agent must be able to start and pull its own input. |
| stdout markers | Fragile parsing. Any unexpected output (library warnings, debug prints) breaks the marker detection. |
| File polling | 1-second latency floor. `readdir` on every group directory every second doesn't scale. File system race conditions with `.tmp` + `rename` workaround. Requires shared volumes (ReadWriteMany PVC) in Kubernetes, which is slow and unreliable. |

All three mechanisms also share a deeper problem: they **couple the Orchestrator and Agent to the same host**. The Agent container must be on the same machine to share stdin/stdout pipes and filesystem mounts. This prevents scheduling Agent containers across a Kubernetes cluster.

## Why NATS?

We evaluated three alternatives:

| Option | Pros | Cons |
|--------|------|------|
| **Redis Streams** | Mature, widely deployed, supports consumer groups | Another stateful service to operate; no native KV with TTL in the same system |
| **gRPC** | Strong typing, bidirectional streaming | Requires generating protobuf stubs; heavy for simple JSON messages; Agent container needs a gRPC server |
| **NATS** | Single binary, zero config; JetStream for durability + KV Store + request-reply in one system; Kubernetes-native; <10MB memory | Less widely known than Redis |

NATS won because it **replaces all three original mechanisms with one system** and provides exactly the primitives we need:

- **KV Store** — for initial input and snapshots (point-read semantics)
- **JetStream** — for streaming results, messages, and tasks (ordered, durable, ack'd)
- **Request-reply** — for close signals (confirmed delivery)

A single NATS server binary with `--jetstream` flag covers everything. Local development: `docker run nats:latest --jetstream`. No configuration files, no clustering setup.

## The 6-Channel Protocol

The Orchestrator and Agent communicate over six logical channels. Each channel has a clear direction, purpose, and NATS primitive:

```
 Orchestrator                                       Agent (container)
 ────────────                                       ──────────────────
                  Channel 1: Initial Input
           ──── KV Store (agent-init) ────→
                  Orch writes before start,
                  Agent reads on startup

                  Channel 2: Streaming Results
           ←─── JetStream (results) ──────
                  Agent publishes result blocks,
                  Orch subscribes

                  Channel 3: Control + Follow-ups
           ──── JetStream (input) ─────────────→   follow-up messages
           ──── JetStream (interrupt) ─────────→   stop signal (current turn)
           ──── Request-Reply (shutdown) ──────→   shutdown signal (close container)

                  Channel 4: Agent Messages
           ←─── JetStream (messages) ─────
                  Agent sends messages to users

                  Channel 5: Task Operations
           ←─── JetStream (tasks) ────────
                  Agent creates/manages tasks

                  Channel 6: Snapshots
           ──── KV Store (snapshots) ─────→
                  Orch writes before start,
                  Agent reads via MCP tools
```

Channel 3 carries three distinct sub-signals (follow-ups, stop, shutdown). Beyond these six channels, RoleMesh added a `safety_events` audit subject on the same `agent-ipc` stream, plus several non-agent NATS namespaces (`web.>`, `approval.*`, `egress.*`) that are documented separately — see "Subject Naming Convention" below for the full inventory.

### Channel 1: Initial Input

**Direction**: Orchestrator → Agent
**NATS primitive**: KV Store, bucket `agent-init`
**Key**: `{job_id}`

Before starting the container, the Orchestrator writes the Agent's initial configuration. The payload (`AgentInitData` in `src/rolemesh/ipc/protocol.py`) is intentionally fat — every piece of state the agent needs to bootstrap rides this one KV entry, so a fresh container has zero additional round-trips before producing output. The fields fall into a few groups:

- **Conversation context** — `prompt`, `chat_jid`, `group_folder`, `session_id`, `is_scheduled_task`
- **Multi-tenant identity** — `tenant_id`, `coworker_id`, `conversation_id`, `user_id`
- **Per-coworker config** — `assistant_name`, `system_prompt`, `role_config`
- **Permissions** — a 4-field dict; see `auth-architecture.md`
- **External MCP** — `mcp_servers`; see `external-mcp-architecture.md`
- **Approval module** — `approval_policies`; see `approval-architecture.md`
- **Safety framework** — `safety_rules` + `slow_check_specs`; see `safety/safety-framework.md`

For each module-specific group, "absent on this run" is encoded by `None`, so the container completely skips registering that module's hook when no policy applies — the IPC contract makes "module disabled" zero-cost.

The Agent reads this once on startup, then begins execution.

**Why KV Store instead of stdin?** The Agent container might start on a different node in Kubernetes. It can't receive a stdin pipe across the network. KV is pull-based — the container starts, reads its config, begins work. The Orchestrator writes the KV entry *before* creating the container, so there's no race condition.

**TTL**: 1 hour. Entries are cleaned up automatically even if the container crashes without reading them.

### Channel 2: Streaming Results

**Direction**: Agent → Orchestrator
**NATS primitive**: JetStream
**Subject**: `agent.{job_id}.results`

Each result block is a JSON message:

```json
{
  "status": "success",
  "result": "Here is the ad performance analysis...",
  "newSessionId": "session-uuid-for-resume",
  "error": null
}
```

The Agent publishes multiple result blocks during execution (streaming). The Orchestrator subscribes and forwards each to the user in real-time. The last message before container exit is the definitive final result.

**Why JetStream instead of stdout markers?** JetStream messages are structured JSON — no marker parsing, no corruption from stray output. Messages are ordered and acknowledged. If the Orchestrator restarts mid-stream, it can replay unacknowledged messages.

**Activity-based timeout**: Each received result resets the Orchestrator's timeout timer. If no result arrives within the timeout period, the container is stopped.

### Channel 3: Control Signals + Follow-ups

**Direction**: Orchestrator → Agent
**NATS primitives**: JetStream (follow-ups + interrupt) + Core NATS Request-Reply (shutdown)

This channel carries three distinct control signals to a running agent:

#### Follow-up messages (JetStream `agent.{job_id}.input`)

When a user sends additional messages while the Agent is still running (idle-waiting for input), the Orchestrator publishes them as JetStream messages:

```json
{"type": "input", "text": "Also check ASIN B08YYY"}
```

The Agent subscribes to this subject. In the Claude SDK backend, follow-up messages are fed into the `MessageStream` that the SDK's `query()` function consumes. In the Pi backend, they are appended to the active session. Either way, the agent sees them as continuation of the conversation, not as a new turn.

#### Stop signal (JetStream `agent.{job_id}.interrupt`)

Aborts the **current turn** without closing the container. The agent subscribes with an ordered consumer + `DeliverPolicy.NEW`, so the message is buffered by JetStream if the agent's event loop is currently busy. UX rationale and the agent-side acknowledgement contract live in `docs/steering-architecture.md` and `docs/backend-stop-contract.md`.

**Why JetStream for stop, not Core NATS?** An earlier prototype used Core NATS pub/sub with request-reply. It worked under light load but broke under the Pi backend's stream-processing load: the Core NATS subscription wasn't always registered when the publish happened, raising `NoRespondersError` and silently dropping the stop. JetStream stores the message and delivers once the consumer is ready — durability matters more than ack latency for this particular signal.

#### Shutdown signal (Core NATS request-reply `agent.{job_id}.shutdown`)

Used when the Orchestrator wants to **close the container itself** — idle timeout, preemption by a higher-priority task, scheduler-driven shutdown after a task completes. The agent acks the request; that ack tells the orchestrator it can now stop the Docker container without truncating in-flight work.

**Why request-reply (Core NATS) for shutdown but JetStream for interrupt?** Shutdown is paired with explicit container teardown on the orchestrator side — latency matters and the container is about to disappear anyway, so durability is irrelevant. Interrupt is the opposite: the container keeps running and the message must survive the agent's busy moments, so durability matters more than ack latency.

### Channel 4: Agent Messages to Users

**Direction**: Agent → Orchestrator
**NATS primitive**: JetStream
**Subject**: `agent.{job_id}.messages`

The Agent can proactively send messages to users (progress updates, notifications) via the `send_message` MCP tool:

```json
{
  "type": "message",
  "chatJid": "tg:12345",
  "text": "Found 3 underperforming campaigns. Analyzing each...",
  "groupFolder": "main",
  "timestamp": "2026-03-28T10:00:00+00:00",
  "sender": null
}
```

The Orchestrator subscribes to `agent.*.messages` (wildcard for all job IDs) with a durable consumer `orch-messages`. It validates authorization against the requesting coworker's `AgentPermissions` (see "Authorization Model" below) and routes the message to the appropriate channel (Telegram, Slack, WebUI, etc.).

### Channel 5: Task Operations

**Direction**: Agent → Orchestrator
**NATS primitive**: JetStream
**Subject**: `agent.{job_id}.tasks`

The Agent can create and manage scheduled tasks via MCP tools:

```json
{
  "type": "schedule_task",
  "taskId": "task-1711612800000-a1b2c3",
  "prompt": "Daily ad performance check",
  "schedule_type": "cron",
  "schedule_value": "0 8 * * *",
  "context_mode": "group",
  "targetJid": "tg:12345",
  "groupFolder": "main"
}
```

Supported operations: `schedule_task`, `pause_task`, `resume_task`, `cancel_task`, `update_task`. (The original NanoClaw also exposed `refresh_groups` and `register_group`, but those were removed during the multi-tenant Auth refactor — group registration is now an admin-side operation that doesn't go through this channel.)

The Orchestrator subscribes to `agent.*.tasks` with durable consumer `orch-tasks`. Authorization is enforced against the requesting coworker's `AgentPermissions` (see "Authorization Model" below).

### Channel 6: Snapshots

**Direction**: Orchestrator → Agent
**NATS primitive**: KV Store, bucket `snapshots`
**Key**: `{tenant_id}.{group_folder}.tasks`

Before starting a container, the Orchestrator writes the current scheduled-tasks snapshot, pre-filtered to what the requesting coworker is permitted to see — the orchestrator does the slicing so the agent gets a ready-to-read list. The agent reads it via the `list_tasks` MCP tool. Permission semantics live in `auth-architecture.md`.

The data is point-in-time — not a live stream. This is appropriate because the Agent needs to query current state, not subscribe to changes; if a task is added or paused while the agent is running, the next container spawn will pick it up.

The original NanoClaw also published a `groups` snapshot for `list_groups` / `register_group`. That was removed during the Auth refactor: group registration moved out of the agent IPC surface, so there is no agent-side need for a groups snapshot anymore.

**Why KV Store instead of JetStream?** Snapshots are "what is the current state right now?" — latest-value-wins semantics. JetStream is for event streams where order and history matter. KV is simpler and semantically correct for this use case.

## Subject Naming Convention

```
agent.{job_id}.results        # Channel 2
agent.{job_id}.input          # Channel 3 (follow-ups)
agent.{job_id}.interrupt      # Channel 3 (Stop button — JetStream)
agent.{job_id}.shutdown       # Channel 3 (close container — Core NATS request-reply)
agent.{job_id}.messages       # Channel 4
agent.{job_id}.tasks          # Channel 5
agent.{job_id}.safety_events  # Safety Framework V2 — fire-and-forget audit events
```

**Why `job_id` instead of `coworker_id`?** A single coworker might have multiple concurrent containers — one handling user messages, another running a scheduled task. `job_id` is unique per container invocation, ensuring precise routing. It's generated as `{group_folder}-{uuid_hex[:12]}` at container creation time.

The JetStream stream `agent-ipc` captures all subjects matching `agent.*.(results|input|interrupt|messages|tasks|safety_events)`. The shutdown signal uses Core NATS (not JetStream) — see Channel 3 for why.

In addition to the `agent.*` namespace described above, RoleMesh's NATS bus also carries:

- `web.>` (`web-ipc` stream) — WebUI traffic between FastAPI and the orchestrator
- `approval.decided.*` / `approval.cancel_for_job.*` (`approval-ipc` stream) — Approval module worker queue and Stop-cascade
- `egress.{rules,identity,mcp}.snapshot.request` — request-reply RPCs the egress gateway calls into the orchestrator at boot
- `egress.mcp.changed`, `safety.rule.changed` — fire-and-forget broadcasts for hot-reloading caches in the gateway and agent containers
- `orchestrator.agent.lifecycle` — agent container started/stopped lifecycle events

Each of these is documented by its owning module (`webui-architecture.md`, `approval-architecture.md`, `safety/safety-framework.md`, `egress/deployment.md`) — they are separate concerns that happen to share the same NATS server.

## NATS Infrastructure

### JetStream Streams

The orchestrator manages one stream for agent IPC; two more streams (`web-ipc`, `approval-ipc`) live alongside it on the same NATS server but are owned by the WebUI and Approval modules respectively — they're documented in those modules.

```python
StreamConfig(
    name="agent-ipc",
    subjects=[
        "agent.*.results",
        "agent.*.input",
        "agent.*.interrupt",
        "agent.*.messages",
        "agent.*.tasks",
        "agent.*.safety_events",
    ],
    max_age=3600.0,  # 1 hour TTL — auto-cleanup
)
```

LIMITS retention (not WorkQueue) because both the Orchestrator and Agent subscribe to different subjects within the same stream — WorkQueue would only allow one consumer per subject.

Stream definitions use `add_stream` with an `update_stream` fallback so older deployments that didn't include `agent.*.interrupt` (added during Steering) or `agent.*.safety_events` (added during Safety V2) get their config updated in place at startup, without stranding subjects on the wrong config during a rolling deploy.

### KV Buckets

```python
KeyValueConfig(bucket="agent-init", ttl=3600.0)   # Channel 1
KeyValueConfig(bucket="snapshots",  ttl=3600.0)   # Channel 6
```

1-hour TTL on both. Entries self-clean even if the consumer crashes.

### Durable Consumers

The Orchestrator creates two durable JetStream consumers for agent IPC fan-in:

- `orch-messages` — `agent.*.messages` (Channel 4)
- `orch-tasks` — `agent.*.tasks` (Channel 5)

Durable consumers survive Orchestrator restarts; unprocessed messages are replayed on reconnection. The Safety and Approval modules register additional durable consumers of their own (e.g. `orch-safety-events`, `orch-approval-cancel`) — they're documented in those modules.

Channels 2 (results) and 3 (follow-ups + interrupt) use ephemeral subscriptions scoped to a specific `job_id` — created when a container starts, unsubscribed when it exits. These don't need durability because they're tied to a single container's lifecycle. The shutdown signal uses Core NATS request-reply, so it has no consumer at all.

## Container Environment Variables

The Orchestrator passes two environment variables to every agent container:

| Variable | Example | Purpose |
|----------|---------|---------|
| `NATS_URL` | `nats://nats:4222` (EC-1+) or `nats://host.docker.internal:4222` (legacy) | NATS server address |
| `JOB_ID` | `main-a1b2c3d4e5f6` | Unique per container invocation |

The Agent uses `NATS_URL` to connect and `JOB_ID` as the routing key for the `agent.{job_id}.*` subjects.

The hostname in `NATS_URL` depends on the network topology. The current default (after EC-1) puts agents on an `Internal=true` bridge with NATS attached as service `nats`; legacy deployments use `host.docker.internal`. The Orchestrator rewrites the URL automatically — full topology in `docs/egress/deployment.md`.

## How Each Side Connects

### Orchestrator Side (`NatsTransport`)

```python
class NatsTransport:
    async def connect(self) -> None:
        self._nc = await nats.connect(url)
        self._js = self._nc.jetstream()
        # Create stream and KV buckets (idempotent)
        await self._js.add_stream(StreamConfig(name="agent-ipc", ...))
        await self._js.create_key_value(KeyValueConfig(bucket="agent-init", ...))
        await self._js.create_key_value(KeyValueConfig(bucket="snapshots", ...))
```

Initialized once at startup, shared across all container invocations.

### Agent Side (in `agent_runner/main.py`)

```python
nc = await nats.connect(NATS_URL)
js = nc.jetstream()

# Channel 1: Read initial input
kv = await js.key_value("agent-init")
entry = await kv.get(JOB_ID)
init_data = AgentInitData.deserialize(entry.value)

# Channel 3: Subscribe to follow-ups (JetStream),
# stop signal (JetStream, ordered + DeliverPolicy.NEW),
# and shutdown signal (Core NATS request-reply).
input_sub = await js.subscribe(f"agent.{JOB_ID}.input")
interrupt_sub = await js.subscribe(
    f"agent.{JOB_ID}.interrupt",
    cb=handle_interrupt,
    ordered_consumer=True,
    deliver_policy=DeliverPolicy.NEW,
)
shutdown_sub = await nc.subscribe(f"agent.{JOB_ID}.shutdown", cb=handle_shutdown)

# Channels 4, 5: Publish via MCP tools
# (fire-and-forget, using asyncio.ensure_future for non-blocking)
```

Each container creates its own NATS connection on startup and closes it on exit.

## MCP Tools as the Agent-Side IPC Interface

The Agent doesn't directly call NATS publish functions. Instead, IPC operations are exposed as **MCP tools** that the LLM can invoke:

| MCP Tool | Channel | NATS Subject / KV Key |
|----------|---------|-----------------------|
| `send_message` | 4 | `agent.{job_id}.messages` |
| `schedule_task` | 5 | `agent.{job_id}.tasks` |
| `pause_task` | 5 | `agent.{job_id}.tasks` |
| `resume_task` | 5 | `agent.{job_id}.tasks` |
| `cancel_task` | 5 | `agent.{job_id}.tasks` |
| `update_task` | 5 | `agent.{job_id}.tasks` |
| `list_tasks` | 6 | KV `snapshots.{tenant_id}.{group_folder}.tasks` |

These tools are registered as an **in-process MCP server** (in the Claude SDK backend, via `create_sdk_mcp_server()`; in the Pi backend, as built-in `AgentTool` instances). Either way:
- No separate process for the MCP server
- Tool calls are direct Python function calls
- The LLM sees them as regular tools with JSON Schema parameters

This design keeps the NATS communication logic contained in one place (`agent_runner/tools/rolemesh_tools.py`) while the LLM interacts with clean, documented tool interfaces.

## Authorization Model

The IPC layer's contract on authorization is one sentence: **Channel 4 / Channel 5 payloads carry `tenantId` + `coworkerId` but never carry the requesting agent's permissions.** The agent_runner sets those fields from `AgentInitData` (not from the LLM), and the orchestrator looks up the authoritative `AgentPermissions` for that coworker before honoring the request. An agent cannot escalate by editing the payload because the payload doesn't claim permissions in the first place. Channel 6 snapshots are similarly pre-filtered orchestrator-side, so even a buggy `list_tasks` call cannot read another tenant's data.

The full permission model — fields, role templates, multi-tenant rationale — lives in `auth-architecture.md`.

## Error Handling and Reliability

### Container crashes

If a container crashes without publishing results, the Orchestrator's timeout fires (default: 5 minutes). The container is cleaned up and the error is reported to the user.

KV entries (`agent-init`, `snapshots`) have 1-hour TTL — they self-clean even without explicit deletion.

### NATS server restart

If NATS restarts, the Orchestrator reconnects (3 retry attempts with 1-second wait). JetStream streams and KV data are persisted to disk, so no messages are lost.

Durable consumers (`orch-messages`, `orch-tasks`) resume from their last acknowledged position after reconnection.

### Orchestrator restart

If the Orchestrator restarts while containers are running:
- Running containers continue (they're independent Docker processes)
- Unacknowledged messages in `orch-messages` and `orch-tasks` are replayed
- Orphan containers are cleaned up on next startup via `DockerRuntime.cleanup_orphans("rolemesh-")`
- Channel 2 (results) subscriptions for in-flight jobs are lost — those containers will time out

### Message ordering

JetStream preserves message order within a subject. Channel 2 results arrive in the order the Agent published them. Channel 4 and 5 messages are processed in order per durable consumer.

## Development Setup

```yaml
# docker-compose.dev.yml
services:
  nats:
    image: nats:latest
    ports:
      - "4222:4222"   # Client connections
      - "8222:8222"   # HTTP monitoring dashboard
    command: ["--jetstream", "--store_dir=/data"]
    volumes:
      - nats-data:/data

volumes:
  nats-data:
```

The monitoring dashboard at `http://localhost:8222` shows active connections, streams, consumers, and KV buckets — useful for debugging IPC issues.

Environment variable: `NATS_URL=nats://localhost:4222` (default, no configuration needed for local development).
