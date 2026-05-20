# Multi-Tenant & Multi-Coworker Architecture

This document explains the design of RoleMesh's multi-tenant, multi-coworker architecture — the reasoning behind the decisions, the trade-offs considered, and the final design.

> **Project lineage.** RoleMesh started as a Python rewrite of [NanoClaw](https://github.com/qwibitai/nanoclaw), a single-user TypeScript Claude assistant. Multi-tenancy was the watershed change — the last step of the NanoClaw-era rewrite, after which the codebase was forked and renamed to RoleMesh. The historical sections below talk about the original single-user shape being replaced; the current implementation, including database-level RLS, is RoleMesh-era.

---

## Background

NanoClaw started as a single-user personal AI assistant: one person, one agent, one chat group. The architecture was simple — a `RegisteredGroup` mapped 1:1 to a chat context, the agent ran in a container, and module-level Python globals tracked everything.

As the project evolved toward a general-purpose **AI Coworker platform**, we needed to support:

- Multiple **organizations** (tenants) sharing the same infrastructure
- Multiple **AI coworkers** per tenant (operations AI, customer service AI, etc.)
- Multiple coworkers of the same type (one per product line, one per region, etc.)
- Each instance interacting with users across **multiple chat channels** simultaneously
- **Multiple human users** within an organization, each with appropriate access

This document describes how we got from the single-user design to the multi-tenant architecture.

---

## Core Concepts

### The Entity Hierarchy

```
Tenant (organization)
│
├── Coworker (AI agent)
│   ├── Carries its own config: system prompt, tools, skills, LLM backend
│   ├── Has its own workspace (files, logs)
│   ├── Identified by independent bot identity per channel
│   └── Can operate in multiple chat groups simultaneously
│
├── Conversation (a Coworker's context in a specific chat group)
│   ├── Has independent session/memory
│   └── requires_trigger flag (on for group chats, off for DMs / Web UI)
│
└── User (human team member)
    └── Can interact with multiple Coworkers
```

### Why These Specific Entities?

**Tenant** is straightforward — organizational isolation boundary.

**Coworker** is the central entity — an AI agent with its own identity, configuration (system prompt, tools, skills, LLM backend), workspace, and concurrency limits. We considered splitting this into a "Role template + Coworker instance" model (where Role defines the shared config and Coworker inherits it), but found it to be over-engineering for the current stage: no code uses template reuse, every coworker creation would require a role to exist first, and the extra table/JOIN/CRUD adds complexity with zero benefit. If template reuse is needed later, adding a `roles` table with a FK is a straightforward addition.

**Conversation** emerged from a specific realization: when the same Coworker operates in multiple Telegram groups, the **file workspace should be shared** (same product data, same codebase) but the **conversation memory should be independent** (different groups discuss different topics).

---

## Design Decisions

### 1. Session Scope: Per-Conversation, Not Per-Coworker

**Decision**: Each Conversation (coworker + chat group combination) has its own session.

**Alternatives considered**:

| Approach | Behavior | Problem |
|----------|----------|---------|
| Per-coworker session | All groups share memory | Group A's discussion leaks into Group B |
| Per-conversation session | Each group has independent memory | Correct isolation ✓ |
| Per-user session | Each human gets their own thread | Breaks group collaboration |

The key insight: a Coworker is like a human employee who works in multiple Slack channels. They remember what was said in each channel separately, but they access the same files and databases regardless of which channel they're in.

This pairs with Decision 6 (Workspace Isolation) to form the complete sharing model. Together they answer: **"When the same Coworker operates in multiple chat groups, what is shared and what is isolated?"**

| Resource | Scope | Why |
|----------|-------|-----|
| **Workspace files** (code, data, reports) | Per-coworker (shared) | Same coworker manages the same product line regardless of which chat group the request came from |
| **Session/memory** (conversation history) | Per-conversation (isolated) | Different groups discuss different topics; mixing them would confuse the agent |
| **Logs** | Per-coworker (shared) | Operational visibility across all conversations |
| **Shared knowledge** (SOPs, manuals) | Per-tenant (read-only) | All coworkers in a tenant access the same reference materials |

This is a deliberate asymmetry. A common mistake would be to make everything per-conversation (full isolation) or everything per-coworker (full sharing). The split reflects how a human employee actually works: they remember conversations separately, but their desk and files are the same no matter who they're talking to.

### 2. Bot Identity: Per-Coworker, Not Per-Tenant

**Decision**: Each Coworker has its own bot identity per channel type (e.g., its own Telegram bot).

**Why not one bot per tenant?** If a tenant has 3 coworkers (Ops AI, CS AI, Logistics AI) sharing one Telegram bot, users in a group would see one bot and need to use keywords like `@ops help` vs `@cs help` to route messages. This is:

- Confusing for users (which command was it again?)
- Fragile (typos break routing)
- Missing visual identity (no distinct avatar/name per coworker)

With per-coworker bots, users see `@acme_ops_bot` and `@acme_cs_bot` as separate entities in their group. They `@mention` the one they want to talk to, just like mentioning a human colleague. In Telegram specifically, creating bots is nearly free (one BotFather command per bot).

**Trade-off**: More bots to manage. But this is a configuration problem, not an architectural one — the Channel Gateway pattern handles it cleanly.

**Important caveat — token deduplication**: While the *conceptual* model is "one bot per coworker", during migration multiple coworkers may share the same bot token. The Gateway must deduplicate by token: **one token = one polling connection**, with messages fanned out to all associated bindings. Creating multiple polling instances for the same token causes platform API conflicts (e.g., Telegram's `Conflict: terminated by other getUpdates request`).

### 3. Channel Gateway Pattern

**Decision**: One Gateway per channel type, managing multiple bot instances.

With N coworkers × M channel types, individual bot connections don't scale. A Gateway is a manager object for one channel type — it handles token deduplication, connection lifecycle, unified message callback, shared error handling and rate limiting. Individual bots become lightweight (just a token + connection) and the Gateway owns the complexity. Implementation details for the WebUI gateway specifically live in [`webui-architecture.md`](webui-architecture.md).

### 4. OrchestratorState: Structured State Over Globals

**Decision**: Replace module-level global variables with a structured `OrchestratorState` class.

**Before** (single-tenant):

```python
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_queue: GroupQueue = GroupQueue()
_channels: list[Channel] = []
```

These globals work for single-tenant because every key is unique. In multi-tenant, a `group_folder` like `"main"` could exist in every tenant. Flat dicts break.

**After** (multi-tenant):

```python
class OrchestratorState:
    tenants: dict[str, Tenant]
    coworkers: dict[str, CoworkerState]     # coworker_id → state

@dataclass
class CoworkerState:
    config: CoworkerConfig
    conversations: dict[str, ConversationState]
```

Everything is keyed by IDs, indexed by tenant and coworker. No ambiguity, no collisions.

### 5. Coworker Configuration: No Template Layer

**Decision**: Each Coworker carries its own complete configuration directly.

```python
@dataclass
class CoworkerConfig:
    name: str                 # display name (trigger derived from this)
    folder: str               # workspace path
    system_prompt: str | None
    tools: list[McpServerConfig]   # external MCP server bindings
    agent_backend: str        # "claude" or "pi"
    max_concurrent: int
    container_config: dict | None
    agent_role: str           # "super_agent" | "agent"
    permissions: AgentPermissions  # 4 fields (see auth-architecture.md)
```

All fields live on the `coworkers` table. No JOIN, no merge, no template layer. If multiple coworkers need the same config, they're configured independently — duplication is acceptable at this scale and is easier to reason about than a template inheritance system.

**Why not a Role template layer?** We initially designed one (`roles` table with FK from `coworkers`), then removed it because: no code used the template-reuse capability, every coworker creation required a role to exist first, and the extra table added complexity with no current benefit. If template reuse becomes necessary (e.g., a management UI for "create 5 operations AIs from the same template"), adding it back is straightforward.

> Two pieces of per-coworker configuration grew their own subsystems and *don't* live as columns on `coworkers`:
> - **Skills** — multi-file capability folders, in dedicated `skills` / `skill_files` tables (the legacy `coworkers.skills` JSONB column was dropped). See [`skills-architecture.md`](skills-architecture.md).
> - **Permissions** — stored as a JSONB column, but the model is shared with users and described in [`auth-architecture.md`](auth-architecture.md).

### 6. Workspace Isolation Model

**Decision**: Filesystem isolation at three levels — per-tenant, per-coworker, per-conversation:

- **Tenant boundary** — `data/tenants/{tenant_id}/` never crosses; nothing inside one tenant's tree mounts into another tenant's container.
- **Coworker workspace** — read-write, shared across all of that coworker's conversations.
- **Conversation session** — read-write, scoped to one chat group's memory.
- **Tenant shared space** — read-only knowledge base (SOPs, manuals, reference data) accessible to every coworker in the tenant.

The actual mount paths and bind-mount mechanics live in [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md) — that's the layer that turns `coworker_id + conversation_id` into a `ContainerSpec`. From the multi-tenant perspective the only thing that matters is the asymmetry: workspace is per-coworker (shared), session is per-conversation (isolated), shared knowledge is per-tenant (read-only).

**Why not per-conversation workspace?** If the same coworker manages ad campaigns from both a Telegram group and a Slack channel, the underlying ad data and scripts are the same. Duplicating the workspace per conversation would cause drift and confusion.

**Why read-only shared space?** The shared knowledge base is curated content that coworkers read but shouldn't modify. Write access would create conflicts between coworkers.

---

## Database-Level Isolation: RLS + Dual-Pool

Application-level `WHERE tenant_id = …` filters were the original isolation mechanism. They are not enough — a single forgotten `WHERE` clause becomes a cross-tenant data leak. The current design enforces tenant isolation at the **database role level**, so the wrong-by-default posture is blocked rather than allowed.

### Two Postgres roles, two pools

```
rolemesh_app      LOGIN NOBYPASSRLS    ← all business code
rolemesh_system   LOGIN BYPASSRLS      ← migrations, system maintenance, cross-tenant resolvers
```

Every tenant-scoped table has Row-Level Security enabled with a policy bound to a Postgres GUC variable, set per-transaction:

```sql
CREATE POLICY p_self_tenant ON coworkers
    USING (tenant_id::text = current_setting('rolemesh.tenant_id', true));
```

The orchestrator exposes two connection helpers in `src/rolemesh/db/_pool.py`:

```python
async with tenant_conn(tenant_id) as conn:
    # rolemesh_app pool; SET LOCAL rolemesh.tenant_id = tenant_id;
    # RLS policies bind — every query implicitly scoped to this tenant.
    rows = await conn.fetch("SELECT * FROM coworkers")  # only this tenant's rows

async with admin_conn() as conn:
    # rolemesh_system pool; BYPASSRLS.
    # Cross-tenant work is intentional and visible at the call site.
    rows = await conn.fetch("SELECT * FROM coworkers")  # every tenant's rows
```

A code review can see, at every call site, which side of the trust boundary a query is on — `tenant_conn(...)` is RLS-enforced; `admin_conn()` is the explicit escape hatch.

### Why RLS over application filters

| Approach | Failure mode |
|---|---|
| Application-level `WHERE tenant_id=...` | One forgotten `WHERE` = cross-tenant leak. Bug surface = every query in the codebase. |
| One Postgres database per tenant | Operationally heavy (provisioning, migrations, monitoring × N tenants). Cross-tenant analytics impossible without sharding-aware tooling. |
| **Postgres RLS + dual-pool** ✓ | Bug surface = the few `admin_conn()` call sites. Code review for "did this query need cross-tenant?" is mechanically possible. |

### What's deferred to a future doc

This section covers the *design intent* and the trust-boundary contract. The full RLS policy catalog (which tables have which policies, function classification A/B/C, special cases like `skill_files` keyed transitively through `skills`) lives inline in `src/rolemesh/db/schema.py` and is referenced by the test suite (`tests/db/test_rls_enforcement.py`, `test_admin_path_isolation.py`, `test_cross_tenant_isolation.py`).

---

## Identity & Permissions

This document is about **how rows are scoped to a tenant**. The orthogonal questions — *which user can use which agent*, *which agent is allowed to do what*, *how a JWT/OIDC identity becomes a tenant context* — are covered in [`auth-architecture.md`](auth-architecture.md). The summary:

- **User roles** (`owner` / `admin` / `member`) gate management actions on the platform itself.
- **Agent permissions** are a 4-field model (`data_scope`, `task_schedule`, `task_manage_others`, `agent_delegate`) carried by every coworker. They control what the agent can do at IPC enforcement time. The IPC contract here — payloads carry `tenantId + coworkerId` and the orchestrator looks up the authoritative permissions — is documented in [`nats-ipc-architecture.md`](nats-ipc-architecture.md).
- **Tenant context propagation** — every business query goes through `tenant_conn(tenant_id)`; the GUC binds RLS, and any query that needs to cross tenants must use `admin_conn()` instead.

---

## Data Model

```
                    ┌──────────┐
                    │  Tenant  │
                    └────┬─────┘
           ┌─────────────┼─────────────┐
           ▼             ▼             ▼
       ┌───────┐   ┌───────────┐  ┌───────┐
       │ User  │   │ Coworker  │  │Shared │
       └───────┘   │(config +  │  │Space  │
                   │ workspace)│  └───────┘
                   └─────┬─────┘
                         │
               ┌─────────┼──────────┐
               ▼                    ▼
       ┌───────────────┐   ┌──────────────┐
       │ChannelBinding │   │ScheduledTask │
       │(bot identity) │   └──────────────┘
       └───────┬───────┘
               │
               ▼
       ┌──────────────┐
       │ Conversation │─── session (independent memory)
       └──────┬───────┘
              │
              ▼
       ┌──────────────┐
       │   Messages   │
       └──────────────┘
```

### Key Relationships

- **Tenant → Coworker**: One-to-many. Each coworker carries its own complete config (prompt, tools, backend).
- **Coworker → ChannelBinding**: One-to-many (one per channel type). Each binding has bot credentials.
- **ChannelBinding → Conversation**: One-to-many. One bot in multiple chat groups.
- **Conversation → Session**: One-to-one. Independent conversation memory.
- **Conversation → Messages**: One-to-many.
- **Coworker → Workspace**: One-to-one. Shared filesystem across all conversations.

### Database Tables

The schema below includes only columns with defined purpose — no placeholder fields for unimplemented features. Every tenant-scoped table has RLS enabled (see "Database-Level Isolation" above).

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `tenants` | `id`, `name`, `slug`, `max_concurrent_containers`, `last_message_cursor` | Organizational boundary and limits |
| `users` | `id`, `tenant_id`, `name`, `role`, `email`, `external_sub` | Human users + auth provider mapping |
| `coworkers` | `id`, `tenant_id`, `name`, `folder`, `agent_backend`, `system_prompt`, `tools` (JSONB), `agent_role`, `permissions` (JSONB), `container_config`, `max_concurrent` | AI agents with full config |
| `channel_bindings` | `id`, `tenant_id`, `coworker_id`, `channel_type`, `credentials`, `bot_display_name` | Per-coworker bot identities |
| `conversations` | `id`, `tenant_id`, `coworker_id`, `channel_binding_id`, `channel_chat_id`, `requires_trigger`, `last_agent_invocation`, `user_id` | Per-chat contexts with independent session |
| `sessions` | `conversation_id` (PK), `tenant_id`, `coworker_id`, `session_id` | Claude SDK / Pi session mapping per conversation |
| `messages` | `id`, `tenant_id`, `conversation_id`, `sender`, `content`, `timestamp`, `input_tokens`, `output_tokens`, `cost_usd`, `model_id` | Chat message history + per-turn usage |
| `scheduled_tasks` | `id`, `tenant_id`, `coworker_id`, `conversation_id`, `prompt`, `schedule_type`, `schedule_value`, `next_run` | Cron / interval / once tasks |
| `task_run_logs` | `id`, `task_id`, `run_at`, `duration_ms`, `status`, `result`, `error` | Task execution history |
| `skills` / `skill_files` | (separate subsystem) | Per-coworker skill folders — see [`skills-architecture.md`](skills-architecture.md) |
| `approval_policies` / `approval_requests` / `approval_audit_log` | (separate subsystem) | Approval module — see [`approval-architecture.md`](approval-architecture.md) |
| `safety_rules` / `safety_decisions` / `safety_rule_audit` | (separate subsystem) | Safety framework — see [`safety/safety-framework.md`](safety/safety-framework.md) |

---

## Message Flow

A user message in a Telegram group becomes a turn through this path:

1. **TelegramGateway** receives via the bot mentioned in the message (one token = one polling instance, fanned out to all associated bindings).
2. **Routing**: `binding_id → coworker_id + tenant_id`; `(binding_id, channel_chat_id) → conversation_id`. The internal routing key is `conversation_id` (a UUID), not `channel_chat_id`.
3. **Inbound filtering** (multi-bot groups): if `requires_trigger` is set and the message doesn't match `@coworker.name`, drop it before storage.
4. **Store** in `messages` (RLS-bound to tenant) and enqueue a turn for processing.
5. **Concurrency check** (global + per-tenant + per-coworker — see [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md)).
6. **Spawn container** with the right workspace, session dir, and shared-space mounts; pass the coworker's permissions through `AgentInitData`.
7. **Agent executes**, results flow back via NATS — see [`nats-ipc-architecture.md`](nats-ipc-architecture.md).
8. **Reply** routes through the originating coworker's binding (by `coworker_id`, not by scanning all bindings for matching `chat_id`).

### Critical Routing Rules (learned from implementation)

1. **`conversation_id` is the internal routing key**, not `channel_chat_id`. In Telegram private chats, the same user talking to 3 different bots produces the same `chat_id` (user ID). Only `conversation_id` (UUID) is globally unique.
2. **Inbound filtering before storage**: in multi-bot groups, every bot receives ALL messages. Each bot must filter — only store messages that match its own trigger pattern. Without this, coworkers accumulate irrelevant messages and activate on triggers meant for other coworkers.
3. **Event-driven, not poll-driven**: inbound messages directly enqueue a turn after storage. The system does NOT rely on a polling cursor — per-tenant cursors cause race conditions when multiple apps receive the same message at slightly different times.
4. **IPC reply routing by coworker**: when the agent sends a message via the `send_message` MCP tool, the reply is routed through the source coworker's own binding, not by scanning all bindings for a matching `chat_id`. This prevents replies going through the wrong bot in private chat scenarios.
5. **Deduplication between output channels**: the agent has two output paths — `send_message` (immediate, via IPC) and the results stream (final, via NATS). The orchestrator tracks texts sent via IPC and skips duplicates in the results stream.

---

## How It Evolved from Single-Tenant

The mapping from original NanoClaw concepts. This whole table belongs to **phase 1** (the NanoClaw-era rewrite, before the project was forked to RoleMesh):

| Original (NanoClaw single-tenant) | Multi-Tenant (current) | What Changed |
|---|---|---|
| `RegisteredGroup` | `Coworker` + `Conversation` | Split: "who" separated from "where" |
| `group.folder` | `coworker.folder` | Path: `groups/x/` → `tenants/{tid}/coworkers/x/` |
| `group.trigger` | Derived from `coworker.name` | Trigger text = coworker name; `conversation.requires_trigger` controls on/off |
| `chatJid` | `conversation.channel_chat_id` | 1:N instead of 1:1 |
| `session` (per group) | `session` (per conversation) | Scope narrowed |
| `ASSISTANT_NAME` | `coworker.name` | Global constant → per-entity config |
| `TRIGGER_PATTERN` | From `coworker.name` | Derived from coworker identity |
| `GroupQueue` | Three-level scheduler | Added tenant + coworker limits |
| `Channel` singleton | `ChannelGateway` | One manager per type, multiple bots |
| Module globals | `OrchestratorState` | Structured, indexed by ID |
| `is_main` (bool) | `agent_role` + `AgentPermissions` | 1 boolean → 4 orthogonal fields (added in phase 2) |
| Application-level `WHERE tenant_id` | Postgres RLS + dual-pool | Trust boundary moved into the database (added in phase 2) |

The migration preserves backward compatibility through `DEFAULT_TENANT = "default"` defaults and a converter for legacy `is_main` payloads on the IPC wire.

---

## What This Architecture Does NOT Do

These are explicitly deferred to future work:

- **A2A collaboration** — coworkers delegating tasks to each other. The `agent_delegate` permission field is a placeholder; runtime enforcement and the delegation IPC are not implemented.
- **Cross-tenant marketplace / shared coworkers** — every coworker today belongs to exactly one tenant. There is no concept of "publish my coworker for other tenants to subscribe to."
- **Per-conversation permission overrides** — permissions live on the coworker, not the conversation. A coworker can't be "read-only in this group, full access in another."
- **Tenant resource quotas beyond container concurrency** — there's a `max_concurrent_containers` per tenant, but no token / spend / API quota; cost telemetry exists in `messages.cost_usd` but is reporting-only.

Correspondingly, the database schema does NOT include columns for unimplemented features — fields are added when their features are built, not as placeholders.

---

## Operational Considerations

### Schema Migration

When upgrading across major changes, the migration path lives inline in `src/rolemesh/db/schema.py` (`_create_schema` is idempotent and handles existing tables in place). Notable migrations the file currently encodes:

- Single-tenant → multi-tenant: detect legacy tables (e.g. `messages` with a `chat_jid` column), read data, drop old tables, create new tables, re-insert.
- `is_admin` boolean → `agent_role` + `permissions` JSONB.
- Inline conversion of `agent_backend = 'claude-code'` legacy values to the canonical `'claude'`.
- Backfill `tenant_id` on `oidc_user_tokens` so RLS can apply post-D10.

The orchestrator runs `_create_schema` on startup; rolling deploys work because every transformation is idempotent.

---

## Related documentation

- [`auth-architecture.md`](auth-architecture.md) — `AgentPermissions`, user roles, OIDC flow, the four authorization interception points
- [`agent-executor-and-container-runtime.md`](agent-executor-and-container-runtime.md) — three-level concurrency control (`GroupQueue`), container mounts, runtime selection
- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) — IPC layer between Orchestrator and Agent containers
- [`skills-architecture.md`](skills-architecture.md) — `skills` / `skill_files` tables (separate from `coworkers`)
- [`approval-architecture.md`](approval-architecture.md) — approval policies and audit log
- [`safety/safety-framework.md`](safety/safety-framework.md) — safety rules and decisions
