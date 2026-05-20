# Frontdesk Agent v1.2 — Implementation Handbook

> Branch: `feat/frontdesk` · Status: frozen design, 5 review corrections merged.
> Audience: the Claude Code session(s) doing the implementation.
>
> This is the **single source of truth** for the entire feature. Each
> phase doc (`phase-a-foundation.md`, `phase-b-delegation-core.md`,
> `phase-c-integration.md`) is a focused scope of work that references
> sections of this handbook.

---

## Table of contents

1. [Goal & shape](#1-goal--shape)
2. [Architecture: sub-conversation route (route B)](#2-architecture-sub-conversation-route-route-b)
3. [Verified facts (do not re-derive)](#3-verified-facts-do-not-re-derive)
4. [Frozen design decisions](#4-frozen-design-decisions)
5. [Data model changes](#5-data-model-changes)
6. [9 implementation steps](#6-9-implementation-steps)
7. [Reference data](#7-reference-data)
8. [Common pitfalls (must avoid)](#8-common-pitfalls-must-avoid)
9. [Known v1 trade-offs (must document)](#9-known-v1-trade-offs-must-document)
10. [Out of v1 scope](#10-out-of-v1-scope)
11. [When to stop and ask](#11-when-to-stop-and-ask)

---

## 1. Goal & shape

Add a coworker shape called **Frontdesk** that serves as the single
user-facing entry point for a tenant. Frontdesk either answers simple
questions itself, or **synchronously** delegates to a domain specialist
(accounting / portfolio / trading / ...) and synthesizes the final
reply.

- Users only talk to frontdesk; specialists are invisible.
- Delegation depth is strictly 1 (`frontdesk → specialist`; no chains).
- Existing multi-tenancy isolation, safety, approval, and OIDC user
  identity pass-through all apply automatically.
- This is an RPC pattern — **no handoff** in v1.

---

## 2. Architecture: sub-conversation route (route B)

Each delegation creates a **child `conversation` row** for the
`(parent_conv, target_coworker)` pair. The target runs in that child
conversation; the frontdesk keeps running in the parent. The two are
linked via `conversations.parent_conversation_id`.

Why this matters: RoleMesh's core invariant is that **one conversation
binds to exactly one coworker** (`sessions.conversation_id` is a single
PK, the message loop dispatches per conversation, etc.). Route B
preserves this invariant. The alternative — letting the target reuse the
parent's `conversation_id` — would break it and require defensive
patches in `set_session`, `on_output`, `send_message`, trigger gating,
approval attribution, and more.

Properties:

- Parent and child conversations have independent `sessions` rows.
- Child conversations **do not enter** `_state.coworkers[*].conversations`.
- Child conversations attach to a `channel_type='internal'` channel
  binding (schema anchor only; WebUI gateway does not subscribe — see
  §9 for the known UX consequence).
- All list queries over `conversations` get an `include_children: bool =
  False` default parameter. Single-ID queries (`get_conversation(id)`)
  do not filter (a child conv is a legal target).

Cost: one new column, one `internal` channel type, one new
`delegations` audit table, plus a UI filter. One-time investment;
everything else relies on default RoleMesh behavior.

---

## 3. Verified facts (do not re-derive)

These are confirmed against current `feat/frontdesk` (commit `89e0ca7`):

1. **`sessions` PK is `conversation_id` single-column** (schema.py:378).
   Sub-conv approach gives each child its own row.
2. **`set_session` is NOT inside `executor.execute()`**. The orchestrator
   `_run_agent` writes it explicitly (`main.py:895-897, 923-925`).
   Delegation goes through a sidepath and **must call `set_session`
   itself**.
3. **`agent_delegate` permission exists**;
   `SUPER_AGENT_DEFAULTS.agent_delegate=True`
   (`auth/permissions.py:108`).
4. **Trigger gating**: super_agent bypasses; delegation doesn't go
   through `_message_loop`, so trigger gating is irrelevant.
5. **Per-coworker in-process MCP tools** register at agent boot;
   permission checks live in tool bodies.
6. **All existing IPC tools are fire-and-forget**. Step 3 adds core NATS
   request-reply to `ToolContext`.
7. **`ContainerAgentExecutor.execute()` does not return when a turn
   completes** — target containers are long-lived NATS query loops. The
   delegation handler **must explicitly call
   `queue.request_shutdown(queue_key)`** to make the container exit.

   **Multi-reply turn shape (Pi backend)**: a turn is split into a
   text-bearing `ResultEvent` (`is_final=False`, `result=<text>`) and a
   batch-final marker (`is_final=True`, `result=None`). The handler
   **must track both kinds separately and merge them**. Claude backend
   emits one event with text — the merge logic must handle both shapes.
8. **`GroupQueue` per-coworker concurrency** is computed from the
   `coworker_id` argument to `enqueue_task`, independent of
   `group_jid`.
9. **`enqueue_task` is fire-and-forget**. To return a result, bridge via
   `asyncio.Future`. **Silent drop** when `_shutting_down=True`
   (scheduler.py:181-182). Also silent skip when same `task_id` is
   already running or queued — use a unique `delegation_id` as
   `task_id`.
10. **`asyncio.wait_for` cannot kill the container**.
    `CONTAINER_TIMEOUT` defaults to **1,800,000 ms = 30 minutes idle
    timer** (not wall-clock, not hard wall).
11. **`submit_proposal` is fire-and-forget**. Approval flow is async.
12. **Safety hooks inside the target container** apply to delegated
    calls just like any other call.
13. **`_state.coworkers[id].config: Coworker`** (post PR #27).
14. **Three-way `role_config` name collision** (all IPC-only): on
    `AgentInput`, `AgentInitData`, and `ToolContext` (added in Step 3).
    There is **no `role_config` field on `Coworker` and no
    `role_config` column on `coworkers`**.
15. **`user_id` pass-through** uses `X-RoleMesh-User-Id` header in MCP
    egress (`claude_backend.py:432`).
16. **WebSocket dispatch is keyed by `(binding_id, chat_id)`**. Child
    conversations sit on `internal` bindings, so target-side internal
    events are silently dropped by the WebUI gateway (known v1
    trade-off).
17. **`_message_loop` iterates `_state.coworkers[*].conversations`**;
    since child convs don't enter `_state`, the loop won't accidentally
    dispatch user messages to them.
18. **`channel_bindings.UNIQUE (coworker_id, channel_type)`** exists
    (schema.py:344). Idempotent `internal` binding creation relies on
    it.
19. **`_state.coworkers` access points are spread across `main.py`**.
    Do NOT hardcode line numbers — they shift with every edit. Use grep
    (see §6 Step 2.5).
20. **During a delegation, the frontdesk is in an active turn**. New
    user messages queue behind the current turn (native SDK
    behavior).
21. **Approval write-back paths through `_channel.send_to_conversation`
    are 8 sites in `approval/`**: `executor.py:204`
    (rejected notice), `executor.py:255` (execution report — the
    primary post-approval message), and 6 sites inside `engine.py`
    (`_send_to_origin` helper + its callers for skipped / cancelled /
    expired / stale). Step 7.7 fixes the fan-out **at the channel
    adapter layer**, not at any of these call sites — a single
    intervention covers all 8.
22. **`_coworker_from_state` (main.py:197) is a lossy partial copy**
    (only 8 out of 14 Coworker fields are preserved: id, tenant_id,
    name, folder, agent_backend, system_prompt, tools, max_concurrent).
    `agent_role`, `status`, `permissions`, `is_frontdesk`,
    `routing_description`, `container_config`, `mcp_servers`, `model`
    are silently reset. Step 2.3 must change this to `return
    cw_state.config`.

---

## 4. Frozen design decisions

1. **Naming**: `frontdesk`, one word, all lowercase. Not `concierge`,
   `front_desk`, `front-desk`, or `general agent`.
2. **Frontdesk shape**: `agent_role='super_agent'` AND
   `is_frontdesk=TRUE`. Both required.
3. **Pattern**: synchronous RPC. No handoff in v1.
4. **Delegation depth strictly 1**:
   - Frontdesk's initial call sends `depth=0`. Handler checks
     `depth >= 1` → False → allowed. Target runs with
     `role_config.delegation_depth=1`.
   - If someone manually enables `agent_delegate=True` on a domain
     agent and it tries to delegate, payload `depth=1`. Handler check
     `depth >= 1` → True → rejected.
   - Two defense layers: (a) domain agents default to
     `agent_delegate=False`, so the tool gate refuses; (b) handler
     enforces `MAX_DELEGATION_DEPTH = 1` explicitly. `A → B` is the
     only allowed shape; `A → B → C` is rejected.
5. **Multi-frontdesk per tenant**: allowed. Catalog is the full tenant.
6. **Parallel delegation within one turn**: allowed. The LLM may emit
   multiple `tool_use` blocks in a single assistant message; the
   handlers run concurrently because each uses a distinct
   `queue_key = f"delegate:{child_conv.id}"`.
7. **Audit table**: `delegations`, v1 has it. Status uses conditional
   UPDATE to guarantee terminal states are not overwritten by late
   events.
8. **Sticky session storage**: reuse existing `sessions` table.
   **Handler MUST call `set_session` explicitly** (fact #2).
9. **Approval is async**: `submit_proposal` returns immediately; the
   delegation does not wait for human approval. The 300s deadline is
   for slow LLMs, not for approval queues.
10. **Approval list UI walks parent**: required. SQL goes up
    `parent_conversation_id` to find approvals attributed to the
    target (which live on the child conv).
11. **Approval outcome fan-out**: required. Done at the channel adapter
    layer (`MessageChannel.send_to_conversation`). When the destination
    is a child conv, also deliver to its parent with metadata
    `{source: "delegation_fanout", via_target_name: ...}`. UI renders
    a `via <target>` chip.
12. **PII**: pass through verbatim. No filtering. `prompt_sha256` is
    audit-dedup only, not a PII shield.
13. **Catalog refresh**: spawn-time static injection;
    `list_agents` tool re-queries on demand within a turn.
14. **Frontdesk permissions**: inherit full `SUPER_AGENT_DEFAULTS`. The
    "frontdesk should only route, not act" rule is enforced via the
    system prompt + the routing-accuracy eval gate, not via permissions
    stripping.
15. **Empty catalog**: frontdesk answers the user directly.
16. **Target identifier**: catalog renders `(id: xxx)`, NOT
    `(folder: xxx)`. The system prompt uses the term "agent id", NOT
    "folder slug". This is to avoid the LLM falling into filesystem
    semantics and (since frontdesk inherits super_agent's bash
    permissions) running `ls trading/` to find the agent — a real
    observed bug.
17. **Transport**: core NATS request-reply, reusing the existing
    connection. No new JetStream consumers.
18. **What the target sees**: the prompt arrives as a user message in
    the target's conversation.
19. **Test coverage**: new files meet the project threshold; no `omit`.
20. **Sub-conversation architecture**: each delegation creates a child
    conversation; children don't enter `_state`.
21. **Capacity heuristic**:
    `required_concurrent ≥ peak_concurrent_user_turns × (1 +
    MAX_PARALLEL_DELEGATIONS_PER_TURN) + buffer`. With
    `MAX_PARALLEL_DELEGATIONS_PER_TURN=3` and `buffer=2`.
    `peak_concurrent_user_turns` is a tenant-operations estimate ("how
    many simultaneous users talking to frontdesks at peak"), NOT the
    count of frontdesk coworkers. Advisory only; doesn't block.
22. **`_coworker_from_state` fix**: change to `return cw_state.config`.
    Step 2.3.
23. **FRONTDESK_RULES system prompt contract**: includes (a) anti
    filesystem-semantics line, and (b) failure-passthrough line — the
    LLM's reply MUST include the specialist name AND the literal
    reason text on `isError=true`.

---

## 5. Data model changes

### 5.1 `conversations`

```sql
ALTER TABLE conversations
  ADD COLUMN IF NOT EXISTS parent_conversation_id UUID NULL
    REFERENCES conversations(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS conversations_by_parent
  ON conversations(parent_conversation_id)
  WHERE parent_conversation_id IS NOT NULL;
```

`NULL` means top-level user conv; non-`NULL` means a delegation child.

### 5.2 `coworkers`

```sql
ALTER TABLE coworkers
  ADD COLUMN IF NOT EXISTS is_frontdesk BOOLEAN DEFAULT FALSE;
ALTER TABLE coworkers
  ADD COLUMN IF NOT EXISTS routing_description TEXT;
```

- `is_frontdesk=TRUE` is only valid when `agent_role='super_agent'`.
  Admin UI enforces this; DB has no `CHECK` constraint to keep schema
  flexible.
- `routing_description` is a domain-agent-authored "capability card"
  read by the frontdesk LLM. Frontdesks themselves leave it blank.

### 5.3 `delegations`

```sql
CREATE TABLE IF NOT EXISTS delegations (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID NOT NULL REFERENCES tenants(id),
  parent_conversation_id   UUID NOT NULL REFERENCES conversations(id),
  child_conversation_id    UUID NOT NULL REFERENCES conversations(id),
  from_coworker_id         UUID NOT NULL REFERENCES coworkers(id),
  target_coworker_id       UUID NOT NULL REFERENCES coworkers(id),
  user_id                  UUID,
  prompt_sha256            TEXT NOT NULL,  -- audit-dedup only; NOT a PII shield
  context_mode             TEXT NOT NULL,
  status                   TEXT NOT NULL,
  error_message            TEXT,
  duration_ms              INT,
  started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at                 TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS delegations_by_tenant_time
  ON delegations(tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS delegations_by_parent_conv
  ON delegations(parent_conversation_id, started_at DESC);

ALTER TABLE delegations ENABLE ROW LEVEL SECURITY;
CREATE POLICY delegations_tenant_isolation ON delegations
  USING (tenant_id = current_setting('rolemesh.tenant_id')::uuid);
```

Status enum (informally): `running | success | error | safety_blocked
| stopped | timeout`. Terminal updates use conditional UPDATE
(`WHERE status='running'`) so late events cannot flip a terminal row.

### 5.4 Channel binding format

Child conversations attach to an `internal` channel binding (one per
target coworker, created idempotently). `channel_chat_id` format:

- **sticky**: `internal:{parent_conv_id}:{target_coworker_id}` —
  fixed, so repeat sticky calls look up the same child conv.
- **isolated**: `internal:{parent_conv_id}:{target_coworker_id}:{uuid4()}` —
  the UUID suffix prevents collisions across multiple isolated calls.

`find_child_conversation` MUST match by **exact `channel_chat_id`**,
not just by the `(tenant, parent, coworker)` triple — otherwise a
prior isolated child (with its UUID-suffixed chat_id) could be picked
up by a later sticky lookup.

### 5.5 What is NOT added

- No separate `delegated_sessions` table — the existing `sessions`
  table (PK = `conversation_id`) handles it naturally.
- No `role_config` column on `coworkers` — `role_config` lives only on
  the IPC path.

---

## 6. 9 implementation steps

The work is divided into 9 commits, each on `feat/frontdesk`, each
must pass `uv run pytest && uv run mypy src && uv run ruff check src
tests` before being committed. Commit prefix: `feat(frontdesk):`.
Always `git commit -s`. **Do not push, do not open a PR proactively.**

The 9 steps split into 3 phases for session boundaries — see the phase
docs. Within a phase, the order below is mandatory.

### Step 1 — DB schema migration

Add the columns and table from §5 to `src/rolemesh/db/schema.py`.
Place them near the existing `conversations` / `channel_bindings`
section so the diff stays logically grouped.

**Verify**: existing tests still green; after `bootstrap()`, new
columns and table are queryable.

---

### Step 2 — DB helpers + dataclass + loader + `_coworker_from_state` fix

#### 2.1 Extend dataclasses

```python
@dataclass
class Coworker:
    ...existing...
    is_frontdesk: bool = False
    routing_description: str | None = None

@dataclass
class Conversation:
    ...existing...
    parent_conversation_id: str | None = None
```

Update `_record_to_coworker / create_coworker / update_coworker` and
`_record_to_conversation` accordingly.

#### 2.2 Add `include_children` to list queries

```python
async def get_all_conversations(
    *, include_children: bool = False,
) -> list[Conversation]:
    ...

async def get_conversations_for_coworker(
    coworker_id: str, *, tenant_id: str, include_children: bool = False,
) -> list[Conversation]:
    ...
```

`get_conversation(id)` (single-ID) is unchanged.

#### 2.3 Fix `_coworker_from_state` (the latent bug)

Change `main.py:_coworker_from_state` to:

```python
def _coworker_from_state(cw_state: CoworkerState) -> Coworker:
    """Return the full Coworker stored in this CoworkerState.

    Post PR #27 the partial-copy version dropped status / agent_role /
    permissions / is_frontdesk / routing_description / container_config —
    a latent bug that frontdesk would surface (catalog injection won't
    fire, permission errors silently swallowed).
    """
    return cw_state.config
```

This is independently valuable. Even if frontdesk never ships, this
fix removes a real bug.

#### 2.4 Create `src/rolemesh/db/delegation.py`

```python
from typing import Literal

ChildConvMode = Literal["sticky", "isolated"]


async def get_or_create_internal_binding(
    *, tenant_id: str, coworker_id: str,
) -> ChannelBinding:
    """Idempotent. Relies on channel_bindings.UNIQUE (coworker_id, channel_type).
    INSERT ... ON CONFLICT DO NOTHING RETURNING + fallback SELECT.
    """


async def find_child_conversation(
    *, tenant_id: str,
    parent_conversation_id: str,
    target_coworker_id: str,
    channel_chat_id: str,  # required: distinguishes sticky vs isolated
) -> Conversation | None:
    """SELECT WHERE tenant_id, parent_conversation_id,
       coworker_id, channel_chat_id all match. LIMIT 1.
    """


async def create_child_conversation(
    *, tenant_id: str,
    parent_conversation_id: str,
    target_coworker_id: str,
    target_internal_binding_id: str,
    user_id: str | None,
    mode: ChildConvMode,
) -> Conversation:
    """channel_chat_id format:
         sticky:    "internal:{parent_conv_id}:{target_coworker_id}"
         isolated:  "internal:{parent_conv_id}:{target_coworker_id}:{uuid4()}"
       
       INSERT ... ON CONFLICT (channel_binding_id, channel_chat_id)
       DO NOTHING RETURNING + fallback SELECT.
       requires_trigger=False.
    """


async def insert_delegation(...) -> str:
    """status='running'. Returns delegation id."""


async def update_delegation_terminal(
    delegation_id: str, *, tenant_id: str,
    status: str, duration_ms: int,
    error_message: str | None = None,
) -> bool:
    """Conditional UPDATE: WHERE id=$1 AND status='running'.
    Returns whether the row was actually updated.
    """


async def cleanup_running_delegations() -> int:
    """Called once at orchestrator startup, BEFORE NATS subscribe.
    Marks any still-'running' rows as 'error' (stale from prior crash).
    Returns the count cleaned up.
    """
```

**Note on naming**: use `mode: ChildConvMode` (a string Literal), not
`is_isolated_run: bool`. Boolean parameters whose name is the negation
of one of the values flip easily at call sites; the Literal mirrors
the tool's `context_mode` and stays readable.

#### 2.5 `_state.coworkers` and conversation query grep audit

**Before writing any code**, run both greps and save the output:

```bash
# A. _state.coworkers access points
grep -rn "_state\.coworkers\|cw_state\.conversations\|cw\.conversations" \
  src/ --include='*.py' | grep -v test > /tmp/audit_state_access.txt

# B. conversation list queries
grep -rn "get_all_conversations\|get_conversations_for_coworker\|FROM conversations" \
  src/ tests/ scripts/ web/ --include='*.py' --include='*.ts' --include='*.tsx' \
  > /tmp/audit_conv_queries.txt
```

Expected counts: ~12-15 entries in A, fewer in B. For every line,
answer:

- Does this site read the `conversations` dict assuming all entries
  are user conversations?
- If yes, is the "child conversations don't enter `_state`" invariant
  upheld here (loader defaults to `include_children=False`)?
- Does any site explicitly need to see child convs? **If so, stop and
  report — don't change the design on a hunch.**

For grep B: does the call pass `include_children=True`? If not, is the
default `False` safe? WebUI direct SQL must `WHERE
parent_conversation_id IS NULL` explicitly.

**The grep audit conclusions go in the Step 2 commit message** — paste
the grep output and annotate each line. This deliverable is more
important than the code itself; future maintainers rely on it.

#### 2.6 Tests

- `tests/db/test_delegation.py` — DB helper scenarios: idempotent
  binding, find with chat_id filter, create with ON CONFLICT,
  delegations row lifecycle, conditional terminal update returning
  False on a row already terminal, cleanup of running rows.
- `tests/core/test_coworker_from_state_full_copy.py` — build a fully
  populated `Coworker` (with `is_frontdesk=True / status='active' /
  permissions / agent_role / model / container_config / ...`), wrap
  in `CoworkerState`, call `_coworker_from_state(cs)`, assert ALL 14
  fields preserved.

**Sizing**: ~160 prod + ~230 tests.

---

### Step 3 — ToolContext core NATS RPC + role_config field

#### 3.1 ToolContext dataclass

```python
@dataclass
class ToolContext:
    js: JetStreamContext
    nc: NATSClient                                              # NEW
    job_id: str
    chat_jid: str
    group_folder: str
    permissions: dict[str, object]
    tenant_id: str
    coworker_id: str
    conversation_id: str
    user_id: str = ""
    mcp_tool_reversibility: dict[str, dict[str, bool]] = field(
        default_factory=dict
    )
    # NEW: per-turn IPC hint. Same shape as AgentInput.role_config but
    # defaults to {} instead of None — consumers can use .get(...) without
    # a None check everywhere.
    role_config: dict[str, object] = field(default_factory=dict)
    _bg_tasks: set[asyncio.Task[None]] | None = None

    async def request(
        self, subject: str, data: dict[str, Any], timeout: float = 320.0,
    ) -> dict[str, Any]:
        msg = await self.nc.request(
            subject, json.dumps(data).encode(), timeout=timeout,
        )
        return json.loads(msg.data.decode())
```

#### 3.2 ToolContext construction site

In `agent_runner/main.py`, the `ToolContext(` construction must
explicitly handle the None case:

```python
ctx = ToolContext(
    js=js,
    nc=nc,                                                       # NEW
    job_id=job_id,
    ...
    # AgentInitData.role_config may be None on the wire; normalize here
    # so consumers downstream never see None. dict(...) also makes a
    # shallow copy — defensive against tools accidentally mutating the
    # init_data dict and back-propagating into IPC state.
    role_config=dict(init_data.role_config or {}),
)
```

#### 3.3 Tests

`tests/agent_runner/test_tool_context.py`:

- `ctx.request(...)` returns correct payload; timeout raises
  `asyncio.TimeoutError`.
- `init_data.role_config=None` → `ctx.role_config == {}`.
- `init_data.role_config={"foo": 1}` → `ctx.role_config == {"foo": 1}`.
- Mutating `ctx.role_config["bar"] = 2` does NOT mutate
  `init_data.role_config` (shallow copy proof).

**Sizing**: ~55 prod + ~80 tests.

---

### Step 4 — `list_agents` tool + handler

#### 4.1 Tool registration

```python
{
    "name": "list_agents",
    "description": (
        "List the domain specialist agents available in this tenant. "
        "Returns name, id, and description. Use when unsure which "
        "specialist matches the user's request, or to refresh your view "
        "of available agents (the static catalog you got at spawn may be "
        "stale if specialists changed since)."
    ),
    "parameters": {"type": "object", "properties": {}},
}

async def list_agents(args, ctx) -> ToolResult:
    payload = {"tenantId": ctx.tenant_id, "fromCoworkerId": ctx.coworker_id}
    try:
        resp = await ctx.request(
            f"agent.{ctx.job_id}.list_agents.request", payload, timeout=10.0,
        )
    except asyncio.TimeoutError:
        return _text_result("list_agents timed out.", is_error=True)
    return _text_result(resp.get("text", ""))
```

No permission gate — any super_agent can call it (useful for
debugging from a frontdesk console).

#### 4.2 `src/rolemesh/orchestration/catalog.py`

```python
def render_agent_catalog(
    state: OrchestratorState, tenant_id: str, *, exclude: str,
) -> str:
    """Render the same-tenant delegatable-specialist roster.

    Uses (id: xxx) NOT (folder: xxx). "folder" triggers LLM filesystem
    semantics; frontdesk inherits super_agent's bash perms and would try
    `ls trading/` to find an agent.
    """
    lines = ["Domain specialists available in this tenant:"]
    for cs in state.coworkers.values():
        c = cs.config
        if (c.tenant_id == tenant_id
            and c.agent_role == "agent"
            and c.status == "active"
            and not c.is_frontdesk
            and c.id != exclude):
            desc = c.routing_description or "(no description provided)"
            lines.append(f"- {c.name} (id: {c.folder}) — {desc}")
    if len(lines) == 1:
        return "No specialists available. Answer the user directly."
    return "\n".join(lines)


FRONTDESK_RULES = """\
You are the front desk of this organization.

Specialists are OTHER AGENTS reachable ONLY through the delegate_to_agent
tool. They are NOT files, directories, processes, or anything you can
access via bash/ls/read/edit. Do NOT try filesystem operations to find
them.

Routing rules:
- For simple greetings or status questions, answer yourself.
- For domain-specific requests, call delegate_to_agent with the
  specialist's agent id (e.g. "trading"). The agent id is a routing
  identifier passed verbatim through the tool, not a filesystem path.
- Write self-contained delegation prompts; specialists cannot see this
  conversation.
- For multi-domain requests, call delegate_to_agent multiple times —
  in parallel within one assistant message if requests are independent,
  or sequentially across turns if a later one needs the earlier one's
  result.
- If you don't see a matching specialist in the catalog above, call
  list_agents first to refresh — the catalog above is from your spawn
  time and may be stale.
- When a specialist returns isError=true (error, safety_blocked, or
  timeout), your reply MUST include both the specialist's name and the
  literal reason text from the tool response. Paraphrasing the reason
  is acceptable; omitting it or replacing it with vague phrasing like
  "had some trouble" is not.

  Example acceptable: "I asked Trading to place the order, but it
  declined: Order size exceeds daily limit for unverified accounts.
  Would you like to try a smaller size?"

  Example NOT acceptable: "I had some trouble; let me try again."
"""
```

#### 4.3 Orchestrator subscription

```python
async def _handle_list_agents_request(msg: Msg) -> None:
    data = json.loads(msg.data.decode())
    rendered = render_agent_catalog(
        _state, data["tenantId"], exclude=data["fromCoworkerId"],
    )
    await msg.respond(json.dumps({"text": rendered}).encode())
```

#### 4.4 Tests

- `tests/integration/test_list_agents.py` — active / paused / cross-tenant
  / self-exclusion / frontdesk-exclusion all rendered correctly.
- `tests/orchestration/test_catalog_no_filesystem_terms.py`:
  ```python
  def test_catalog_no_filesystem_terms():
      catalog = render_agent_catalog(...)
      lower = catalog.lower()
      assert "folder" not in lower
      assert "directory" not in lower
      assert "(id:" in catalog

  def test_frontdesk_rules_no_filesystem_terms():
      assert "folder slug" not in FRONTDESK_RULES.lower()
      assert "MUST include both the specialist's name" in FRONTDESK_RULES
  ```

**Sizing**: ~120 prod + ~150 tests.

---

### Step 5 — `delegate_to_agent` tool + handler (the core)

#### 5.1 Tool registration

```python
{
    "name": "delegate_to_agent",
    "description": (
        "Delegate the user's request to a domain specialist and return "
        "their answer.\n\n"
        "RULES:\n"
        "- Identify target by its agent id (e.g. 'trading'). Not a path.\n"
        "- Write a self-contained prompt; the target cannot see this "
        "conversation.\n"
        "- Use 'isolated' for one-shot questions; 'sticky' for multi-turn "
        "workflow with same specialist.\n"
        "- You may call this multiple times per turn, including in "
        "parallel.\n"
        "- If isError=true, your reply MUST quote the literal reason. "
        "See system prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "prompt": {"type": "string"},
            "context_mode": {
                "type": "string", "enum": ["isolated", "sticky"],
                "default": "isolated",
            },
        },
        "required": ["target", "prompt"],
    },
}


MAX_DELEGATE_PROMPT_CHARS = 16_000

async def delegate_to_agent(args, ctx) -> ToolResult:
    if not ctx.permissions.get("agent_delegate"):
        return _text_result(
            "Permission denied: agent_delegate is not enabled.", is_error=True,
        )
    target = args.get("target", "").strip()
    prompt = args.get("prompt", "")
    context_mode = args.get("context_mode") or "isolated"
    if not target or not prompt:
        return _text_result("target and prompt are required.", is_error=True)
    if len(prompt) > MAX_DELEGATE_PROMPT_CHARS:
        return _text_result(
            f"prompt exceeds {MAX_DELEGATE_PROMPT_CHARS} chars.", is_error=True,
        )
    if context_mode not in ("isolated", "sticky"):
        return _text_result(
            "context_mode must be 'isolated' or 'sticky'.", is_error=True,
        )

    payload = {
        "type": "delegate_to_agent",
        "tenantId": ctx.tenant_id,
        "fromCoworkerId": ctx.coworker_id,
        "fromConversationId": ctx.conversation_id,
        "userId": ctx.user_id or None,
        "target": target,
        "prompt": prompt,
        "contextMode": context_mode,
        "depth": int(ctx.role_config.get("delegation_depth", 0)),
    }
    try:
        resp = await ctx.request(
            f"agent.{ctx.job_id}.delegate.request", payload, timeout=320.0,
        )
    except asyncio.TimeoutError:
        return _text_result(
            f"Delegation to '{target}' timed out at the RPC layer.",
            is_error=True,
        )
    return _text_result(
        resp.get("text", ""), is_error=bool(resp.get("isError", False)),
    )
```

#### 5.2 Defensive `send_message` guard

```python
async def send_message(args, ctx) -> ToolResult:
    if ctx.role_config.get("is_delegated_call"):
        return _text_result(
            "send_message is not allowed inside a delegated call.",
            is_error=True,
        )
    # ... existing logic ...
```

#### 5.3 Orchestrator handler — `src/rolemesh/orchestration/delegation.py`

This handler follows the same pattern as
`task_scheduler.py:_run_task`: `enqueue_task` with a closure,
`executor.execute()` inside the closure, `asyncio.Future` bridging the
result back out. **Do not add new methods to `GroupQueue`.**

```python
import asyncio, hashlib, json, time
from rolemesh.agent.executor import AgentInput, AgentOutput
from rolemesh.core.orchestrator_state import OrchestratorState
from rolemesh.container.scheduler import GroupQueue
from rolemesh.db.chat import set_session, get_session
from rolemesh.db.delegation import (
    get_or_create_internal_binding, find_child_conversation,
    create_child_conversation, insert_delegation,
    update_delegation_terminal,
)
from rolemesh.orchestration.catalog import render_agent_catalog


# Delegation depth is strictly 1. Semantics of `depth`:
#   depth=0  → the caller is the top-level (frontdesk) → allow.
#   depth>=1 → the caller is already a delegate → refuse to chain.
MAX_DELEGATION_DEPTH = 1

DEFAULT_BUSINESS_DEADLINE_S = 300.0

# Outer guard ONLY covers "the closure never ran" (GroupQueue silently
# dropped the task, event loop wedged, etc.). The business timeout
# (slow LLM, etc.) is covered by the inner wait_for(execute, 300s)
# in the closure itself. These two timers DO NOT stack.
OUTER_GUARD_S = 30.0


async def handle_delegate_request(
    msg, *, state, queue, get_executor,
) -> None:
    response = await _process_one(msg, state, queue, get_executor)
    try:
        await msg.respond(json.dumps(response).encode())
    except Exception:
        log.exception("Failed to respond to delegate request")


async def _process_one(msg, state, queue, get_executor) -> dict:
    data = json.loads(msg.data.decode())

    # ------- 1. Parse + validate -------
    from_id = data["fromCoworkerId"]
    parent_conv_id = data["fromConversationId"]
    target_slug = data["target"]
    ctx_mode = data["contextMode"]
    depth = int(data.get("depth", 0))
    user_id = data.get("userId")

    from_cs = state.coworkers.get(from_id)
    if from_cs is None:
        return _err("Calling coworker not found.")
    from_co = from_cs.config
    if from_co.tenant_id != data["tenantId"]:
        return _err("Tenant mismatch.")
    if not (from_co.permissions and from_co.permissions.agent_delegate):
        return _err(f"{from_co.name} cannot delegate.")
    if depth >= MAX_DELEGATION_DEPTH:
        return _err(f"Delegation depth {depth} exceeds limit (max 1 hop).")

    # ------- 2. Resolve target -------
    target_co = _resolve_target(
        state, from_co.tenant_id, target_slug, from_co.id,
    )
    if target_co is None:
        catalog = render_agent_catalog(
            state, from_co.tenant_id, exclude=from_co.id,
        )
        return _err(f"Agent '{target_slug}' not found.\n\n{catalog}")

    # ------- 3. Idempotent internal binding -------
    binding = await get_or_create_internal_binding(
        tenant_id=target_co.tenant_id, coworker_id=target_co.id,
    )

    # ------- 4. Find or create child conv (chat_id distinguishes mode) -------
    sticky_chat_id = f"internal:{parent_conv_id}:{target_co.id}"
    if ctx_mode == "sticky":
        child_conv = await find_child_conversation(
            tenant_id=target_co.tenant_id,
            parent_conversation_id=parent_conv_id,
            target_coworker_id=target_co.id,
            channel_chat_id=sticky_chat_id,
        )
        if child_conv is None:
            child_conv = await create_child_conversation(
                tenant_id=target_co.tenant_id,
                parent_conversation_id=parent_conv_id,
                target_coworker_id=target_co.id,
                target_internal_binding_id=binding.id,
                user_id=user_id, mode="sticky",
            )
    else:
        child_conv = await create_child_conversation(
            tenant_id=target_co.tenant_id,
            parent_conversation_id=parent_conv_id,
            target_coworker_id=target_co.id,
            target_internal_binding_id=binding.id,
            user_id=user_id, mode="isolated",
        )

    # ------- 5. Resolve session_id -------
    session_id: str | None = None
    if ctx_mode == "sticky":
        session_id = await get_session(
            child_conv.id, tenant_id=target_co.tenant_id,
        )

    # ------- 6. Insert audit row -------
    delegation_id = await insert_delegation(
        tenant_id=target_co.tenant_id,
        parent_conversation_id=parent_conv_id,
        child_conversation_id=child_conv.id,
        from_coworker_id=from_co.id,
        target_coworker_id=target_co.id,
        user_id=user_id,
        prompt_sha256=hashlib.sha256(data["prompt"].encode()).hexdigest(),
        context_mode=ctx_mode,
    )
    started = time.monotonic()

    # ------- 6b. Refuse early if queue shutting down -------
    if getattr(queue, "_shutting_down", False):
        duration_ms = int((time.monotonic() - started) * 1000)
        await update_delegation_terminal(
            delegation_id, tenant_id=target_co.tenant_id,
            status="error", duration_ms=duration_ms,
            error_message="GroupQueue is shutting down; delegation refused.",
        )
        return _err("GroupQueue is shutting down; delegation refused.")

    # ------- 7. Build AgentInput -------
    agent_input = AgentInput(
        prompt=data["prompt"],
        group_folder=target_co.folder,
        chat_jid=child_conv.channel_chat_id,
        permissions=target_co.permissions.to_dict(),
        tenant_id=target_co.tenant_id,
        coworker_id=target_co.id,
        conversation_id=child_conv.id,
        user_id=user_id or "",
        session_id=session_id,
        is_scheduled_task=False,
        assistant_name=target_co.name,
        system_prompt=target_co.system_prompt,
        role_config={
            "is_delegated_call": True,
            "delegated_by": from_co.id,
            "delegation_depth": depth + 1,
            "parent_conversation_id": parent_conv_id,
            "delegation_id": delegation_id,
        },
    )

    # ------- 8. Run + collect events -------
    # Pi backend splits success into text-bearing (is_final=False) +
    # marker (is_final=True, result=None). Claude backend emits one
    # event with text. Track both and merge.
    text_event: AgentOutput | None = None
    final_marker: AgentOutput | None = None
    terminal_event: AgentOutput | None = None
    result_future: asyncio.Future[None] = asyncio.Future()
    queue_key = f"delegate:{child_conv.id}"

    executor = get_executor(target_co.agent_backend)
    if executor is None:
        duration_ms = int((time.monotonic() - started) * 1000)
        await update_delegation_terminal(
            delegation_id, tenant_id=target_co.tenant_id,
            status="error", duration_ms=duration_ms,
            error_message=f"No executor for {target_co.agent_backend}",
        )
        return _err(f"No executor for backend {target_co.agent_backend}.")

    async def _on_output(out: AgentOutput) -> None:
        nonlocal text_event, final_marker, terminal_event
        try:  # never propagate; on_output failures must not poison audit
            if out.status == "success" and not out.is_final:
                if out.result:
                    text_event = out
            elif out.status == "success" and out.is_final:
                final_marker = out
                # Critical: the target container will not exit on its own.
                queue.request_shutdown(queue_key)
                if not result_future.done():
                    result_future.set_result(None)
            elif out.status in ("error", "safety_blocked", "stopped"):
                terminal_event = out
                queue.request_shutdown(queue_key)
                if not result_future.done():
                    result_future.set_result(None)
        except Exception:
            log.exception("on_output handler raised; not propagating")

    def _on_process(container_name: str, job_id: str) -> None:
        return None

    async def _closure() -> None:
        try:
            await asyncio.wait_for(
                executor.execute(agent_input, _on_process, _on_output),
                timeout=DEFAULT_BUSINESS_DEADLINE_S,
            )
            if not result_future.done():
                # Closure finished but no terminal event seen — treat as success
                # with empty result. (Rare; mostly happens if the backend exits
                # cleanly without emitting any AgentOutput.)
                result_future.set_result(None)
        except asyncio.TimeoutError as e:
            # Business deadline tripped. Tell the container to shut down.
            queue.request_shutdown(queue_key)
            if not result_future.done():
                result_future.set_exception(e)
        except Exception as e:
            if not result_future.done():
                result_future.set_exception(e)

    queue.enqueue_task(
        queue_key, f"delegate-{delegation_id}", _closure,
        tenant_id=target_co.tenant_id, coworker_id=target_co.id,
    )

    # ------- 9. Wait — OUTER_GUARD only covers "closure never ran" -------
    try:
        await asyncio.wait_for(result_future, timeout=OUTER_GUARD_S)
        # Closure finished normally (or with a business-layer exception
        # bubbling out via set_exception).
        final_out = _merge_pi_two_event_pattern(
            text_event, final_marker, terminal_event,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        response, status = _map_output_to_response(
            final_out, target_co, delegation_id, child_conv.id,
        )

        # Sticky: persist new_session_id. Defensive: DB hiccup is not fatal —
        # the target actually succeeded; we log and let the next sticky
        # call start fresh.
        if (ctx_mode == "sticky"
            and final_out.status == "success"
            and final_out.new_session_id):
            try:
                await set_session(
                    child_conv.id, target_co.tenant_id, target_co.id,
                    final_out.new_session_id,
                )
            except Exception as e:
                log.warning(
                    "sticky session save failed; next sticky call from "
                    "this parent/target starts fresh session",
                    delegation_id=delegation_id, error=str(e),
                )

        await update_delegation_terminal(
            delegation_id, tenant_id=target_co.tenant_id,
            status=status, duration_ms=duration_ms,
            error_message=response.get("text") if status != "success" else None,
        )
        return response

    except asyncio.TimeoutError:
        # OUTER_GUARD tripped — the closure never ran (queue stalled).
        # Note: a business-layer 300s timeout would arrive here via the
        # except Exception branch (set_exception(TimeoutError)) — not via
        # this branch. The audit message must distinguish.
        duration_ms = int((time.monotonic() - started) * 1000)
        await update_delegation_terminal(
            delegation_id, tenant_id=target_co.tenant_id,
            status="error", duration_ms=duration_ms,
            error_message="Delegation task never started (queue stalled).",
        )
        return _err("Delegation task failed to start.")

    except Exception as e:
        # The closure raised (including business-layer TimeoutError
        # surfaced via set_exception).
        is_business_timeout = isinstance(e, asyncio.TimeoutError)
        duration_ms = int((time.monotonic() - started) * 1000)
        status_str = "timeout" if is_business_timeout else "error"
        msg = (
            f"{target_co.name} took too long; aborted."
            if is_business_timeout else str(e)
        )
        await update_delegation_terminal(
            delegation_id, tenant_id=target_co.tenant_id,
            status=status_str, duration_ms=duration_ms,
            error_message=msg,
        )
        if is_business_timeout:
            return _timeout_response(
                target_co, delegation_id, child_conv.id, duration_ms,
            )
        return _error_response(
            target_co, delegation_id, child_conv.id, duration_ms, str(e),
        )


def _merge_pi_two_event_pattern(
    text_event: AgentOutput | None,
    final_marker: AgentOutput | None,
    terminal_event: AgentOutput | None,
) -> AgentOutput:
    if terminal_event is not None:
        return terminal_event
    result = text_event.result if text_event else None
    new_sid = (
        final_marker.new_session_id if final_marker
        else (text_event.new_session_id if text_event else None)
    )
    return AgentOutput(
        status="success", result=result, new_session_id=new_sid,
        is_final=True,
    )
```

Helpers `_resolve_target / _map_output_to_response / _err /
_timeout_response / _error_response` live in the same file.
`_resolve_target` filters: same tenant, `status='active'`,
`agent_role='agent'`, not `is_frontdesk`, not self; matches by
`folder`.

`_map_output_to_response`:

| `AgentOutput.status` | RPC `status` | RPC `text` | `isError` |
|---|---|---|---|
| `success` | `success` | `output.result` | false |
| `error` | `error` | `"<target.name> failed: <output.error>"` | true |
| `safety_blocked` | `safety_blocked` | `"<target.name> declined: <output.result>"` | true |
| `stopped` | `error` | `"<target.name> was interrupted."` | true |

#### 5.4 NATS subscription + startup ordering

```python
# 1. DB pool init
# 2. cleanup_running_delegations()       ★ MUST be before subscribe
# 3. _load_state_from_db()               # default include_children=False
# 4. NATS subscriptions
# 5. _message_loop
await cleanup_running_delegations()
await _load_state_from_db()
await transport.nc.subscribe(
    "agent.*.delegate.request", cb=_handle_delegate_request,
)
await transport.nc.subscribe(
    "agent.*.list_agents.request", cb=_handle_list_agents_request,
)
```

#### 5.5 Tests — the 22-scenario matrix

`tests/integration/test_delegate_to_agent.py` must cover:

1. **happy path** — child conv created with
   `parent_conversation_id`, `requires_trigger=False`; final reply
   reaches frontdesk; audit row `status='success'`.
2. **permission rejected** — both agent-side gate and
   orchestrator-side gate fire.
3. **self-delegation rejected**.
4. **cross-tenant rejected**.
5. **target not found** — error text contains the catalog.
6. **depth limit**:
   - `depth=0` payload (frontdesk's initial call) → allowed.
   - `depth=1` payload (caller is already a delegate) → rejected.
7. **target is frontdesk rejected**; **target is super_agent rejected**.
8. **isolated** — `session_id=None`; each call creates a new child conv.
9. **sticky round-trip** (core):
   - 1st call: target emits `new_session_id=S1`; handler **explicitly
     calls `set_session(child_conv.id, S1)`**.
   - Assert: `await get_session(child_conv.id) == S1`.
   - Assert: `await get_session(parent_conv.id) == BEFORE_DELEGATE` —
     **parent's sessions row must NOT change**. (Mutation test
     value: catches a bug where handler accidentally writes
     `set_session(parent_conv.id, ...)`.)
   - 2nd call: handler finds same child conv, pulls S1 from
     `sessions`, passes `resume=S1` to the SDK.
   - **A3 regression**: an isolated call FIRST, then a sticky call —
     sticky creates a NEW child conv (does not reuse the isolated one).
10. **safety_blocked passthrough** — frontdesk receives
    `safety_blocked` + `isError=true` + reason text intact.
11. **business deadline** — target stub `await asyncio.sleep(400)` →
    inner `wait_for(300s)` trips → `set_exception(TimeoutError)` →
    handler's `except Exception` branch with `is_business_timeout=True`
    → audit row `status='timeout'`, message contains `"took too long"`.
12. **audit idempotency** — simulate a late `success` arriving AFTER
    a `timeout` was already written:
    - Step a: call `update_delegation_terminal(id, status='timeout')`.
    - Step b: call `update_delegation_terminal(id, status='success')`.
    - Assert: step b returns `False`. Assert: DB row remains
      `status='timeout'`.
13. **multi-reply target merge (A2)** — Pi-style two-event success:
    - Event 1: `AgentOutput(status='success', is_final=False, result='Hi')`.
    - Event 2: `AgentOutput(status='success', is_final=True, result=None,
      new_session_id='S')`.
    - Assert: response text = `'Hi'`; new_session_id captured = `'S'`.
14. **parallel delegation** — one turn, two delegates to different
    targets, both complete concurrently.
15. **OIDC pass-through** — stub MCP receiver asserts
    `X-RoleMesh-User-Id` header equals the parent's `user_id`.
16. **`send_message` blocked inside delegated call** — explicit refusal
    with `is_error=True`.
17. **role_config isolation** — split into two parts:
    - **A. Unit test on handler construction** (~30 lines): factor out
      `_build_agent_input(...)` from `_process_one`, call it with
      synthetic inputs, assert returned `AgentInput.role_config` is
      **exactly equal** to:
      ```python
      {
          "is_delegated_call": True,
          "delegated_by": <from_id>,
          "delegation_depth": 1,
          "parent_conversation_id": <parent_id>,
          "delegation_id": <id>,
      }
      ```
    - **B. Integration via AgentInitData interception** (~60 lines):
      before running, grep `AgentInitData` in `agent_runner/` to find
      the exact subject/KV key it's written to. The test fixture
      subscribes there, runs a real delegation, captures the
      `AgentInitData` arriving at the target container, asserts
      `init_data.role_config` matches the dict above.
18. **GroupQueue shutting down** — set `queue._shutting_down=True`:
    - Handler does NOT call `enqueue_task`.
    - Handler returns error response directly.
    - Audit row written with `status='error'`,
      `error_message='GroupQueue is shutting down; delegation refused.'`.
19. **child conv NOT in `_state.coworkers`** — run `_message_loop` one
    tick; assert child conv id is absent from the target's
    `cs.config.conversations` (and from `_state.coworkers[target].*`
    overall).
20. **sticky concurrency race** — `asyncio.gather` two concurrent
    sticky calls to same `(parent, target)`. Both succeed. Exactly one
    child conv row exists. Both reuse the same session_id after the
    first persists.
21. **explicit `request_shutdown` (A1)** — wrap `queue.request_shutdown`
    in a counting Mock; assert it was called on EACH terminal path:
    success, error, safety_blocked, business timeout. Also assert
    `result_future.get_loop() is asyncio.get_running_loop()` (single
    event loop guarantee).
22. **OUTER_GUARD trip vs business timeout — distinct audit**:
    - Variant a: monkey-patch `queue.enqueue_task` to a no-op (closure
      never runs). After OUTER_GUARD_S=30, audit row has
      `status='error'`,
      `error_message='Delegation task never started (queue stalled).'`.
    - Variant b: closure runs but business deadline trips (test 11).
      Audit row has `status='timeout'`, message contains
      `'took too long'`.
    - The two `error_message` strings MUST be distinct so ops can tell
      them apart.

**Sizing reality check**: 22 tests × ~60-80 lines each with
testcontainers/NATS setup is **~1300 lines of test code**, not the
~800 a casual estimate would produce. Budget accordingly.

**Sizing**: ~440 prod + ~1300 tests.

---

### Step 6 — Frontdesk catalog injection

Find the `AgentInitData(` construction site in
`agent/container_executor.py`. When `coworker.is_frontdesk` is true,
append the catalog + FRONTDESK_RULES to `system_prompt`:

```python
from rolemesh.orchestration.catalog import render_agent_catalog, FRONTDESK_RULES

if coworker.is_frontdesk:
    catalog = render_agent_catalog(
        state, coworker.tenant_id, exclude=coworker.id,
    )
    appended = f"{catalog}\n\n{FRONTDESK_RULES}"
    base = coworker.system_prompt or ""
    effective_system_prompt = f"{base}\n\n{appended}" if base else appended
```

**Prerequisite**: Step 2.3 (`_coworker_from_state` fix) must be in.
Otherwise `coworker.is_frontdesk` is always False at this point.

**Tests**: `tests/integration/test_frontdesk_spawn.py`:

- `is_frontdesk=False` → no catalog appended.
- `is_frontdesk=True` → catalog + FRONTDESK_RULES present in
  effective system prompt.
- Empty catalog (no other agents in tenant) → "No specialists
  available. Answer the user directly." is the catalog body.
- **A6 regression**: `executor.get_coworker(target_id)` returns a
  `Coworker` whose `is_frontdesk == True`, `permissions` is correct,
  `status` is correct. This catches any regression of the
  `_coworker_from_state` fix.

**Sizing**: ~70 prod + ~100 tests.

---

### Step 7 — WebUI admin + approval fan-out + parent-walk

#### 7.1 Schemas + endpoints

Add `is_frontdesk` and `routing_description` to the coworker
admin schema. Validation: `is_frontdesk=True` requires
`agent_role='super_agent'`; otherwise return 400.

#### 7.2 Capacity advisory

```python
def check_capacity(tenant, peak_concurrent_user_turns=3) -> str | None:
    MAX_PARALLEL_DELEGATIONS_PER_TURN = 3
    BUFFER = 2
    required = (
        peak_concurrent_user_turns
        * (1 + MAX_PARALLEL_DELEGATIONS_PER_TURN)
        + BUFFER
    )
    if tenant.max_concurrent_containers < required:
        return (
            f"建议租户容器并发 ≥ {required}（按 {peak_concurrent_user_turns} 个"
            f"高峰并发用户对话 × (1 frontdesk + {MAX_PARALLEL_DELEGATIONS_PER_TURN} "
            f"parallel delegations) + {BUFFER} buffer）。当前 "
            f"{tenant.max_concurrent_containers}。"
        )
    return None
```

UI lets the admin enter "expected peak concurrent active user
conversations". Warning is advisory; never blocks.

#### 7.3 User conversation list endpoint

Confirm it uses default `include_children=False`. Child convs MUST NOT
appear in any user-facing conversation list.

#### 7.4 Approval list parent-walk

```sql
SELECT * FROM approval_requests
WHERE conversation_id = $1
   OR conversation_id IN (
        SELECT id FROM conversations WHERE parent_conversation_id = $1
      )
ORDER BY created_at DESC
```

This lets a user, viewing their parent conversation, see approvals
that the target submitted while running in a child conv.

#### 7.5 UI changes

- `is_frontdesk` toggle (visible only when `agent_role='super_agent'`)
  + `routing_description` textarea (visible only for domain agents).
- Frontdesk detail page: a read-only panel **"Domain specialists
  available to all frontdesks in this tenant"** (emphasize "shared
  across frontdesks" — there is no per-frontdesk ACL).
- User conversation list naturally filters child convs (via §7.3).
- `tool_use` event renders `Frontdesk → <target>` chip when
  `metadata.tool == "delegate_to_agent"`; parent chip has a
  duration timer (target's internal progress is invisible in v1 — the
  timer is what tells the user the system is alive).

#### 7.6 Approval outcome fan-out (the v1.2 correction)

**Problem**: when an approval is rejected, executed, expired, etc.,
the approval module calls
`_channel.send_to_conversation(request.conversation_id, ...)`. For a
delegated `submit_proposal`, `request.conversation_id == child_conv.id`,
which sits on an `internal` binding. The WebUI gateway silently drops
the message → the user never sees the outcome. **Blocking UX bug**.

**Fix at the channel adapter layer** (NOT inside `approval/`). There
are 8 distinct `send_to_conversation` call sites under `approval/`;
fixing the adapter is one intervention that covers them all.

Locate the implementation of `MessageChannel.send_to_conversation`
(grep for `send_to_conversation` def — there is typically one in
`main.py` around the orchestrator `_channel` builder, and one
in `approval/notification.py`). Update it to:

```python
async def send_to_conversation(
    self, conversation_id: str, message: str,
    metadata: dict | None = None,
) -> None:
    # Original path: deliver to the conversation itself. Preserves
    # audit completeness and the existing per-conv message stream
    # (e.g. admin browsing the child conv still sees the message).
    await self._dispatch(conversation_id, message, metadata=metadata)

    # Fan-out: if this is a delegation child conv, also deliver to
    # parent so the user sees it.
    conv = await get_conversation(conversation_id)
    if conv and conv.parent_conversation_id:
        target_co = await get_coworker(
            conv.coworker_id, tenant_id=conv.tenant_id,
        )
        target_name = target_co.name if target_co else "specialist"
        await self._dispatch(
            conv.parent_conversation_id,
            message,
            metadata={
                **(metadata or {}),
                "source": "delegation_fanout",
                "via_target_coworker_id": conv.coworker_id,
                "via_target_name": target_name,
            },
        )
```

Frontend `message-item` component renders `metadata.source ==
"delegation_fanout"` with a `via <target_name>` chip to distinguish
from a normal assistant message.

**Why channel-adapter and not engine-side helper**:

1. There are 8 call sites in `approval/` (`executor.py:204` rejected,
   `executor.py:255` execution report, plus 6 inside `engine.py`).
   Wrapping each by hand will miss one.
2. `send_to_conversation` semantically means "the final user-visible
   message for this conversation". Fan-out to the parent IS that
   semantic — children with `internal` bindings have no UI surface.
3. Any future non-approval path that targets a child conv (errors,
   notifications, scheduled task misroute) gets fan-out for free.

**Non-regression**: a regular conversation has
`parent_conversation_id IS NULL`, so the fan-out branch is skipped.

#### 7.7 Tests

`tests/webui/test_admin_frontdesk.py`:

- `is_frontdesk=True` requires `agent_role='super_agent'`; 400
  otherwise.
- `routing_description` editable.
- Capacity advisory returned as warning, doesn't block save.
- Conversation list endpoint returns NO child convs by default.
- Approval parent-walk: insert an `approval_requests` row attributed
  to a child conv; `GET /api/conversations/{parent}/approvals`
  returns it.

`tests/integration/test_approval_fanout.py`:

- **Approved + executed report** — manually build a child conv (parent
  non-null, coworker is `trading`). Attach an `approval_request` row.
  Trigger the `executor.py:255` path. Assert messages exist in BOTH
  child and parent conv rows. Parent row metadata contains
  `via_target_name="trading"`.
- **Rejected notice** — same but trigger `executor.py:204`. Both
  conv rows get the rejection message.
- **Skipped notice** — trigger `engine._send_to_origin` via the
  skipped flow. Both conv rows get the skipped message.
- **Non-regression** — plain conversation (`parent_conversation_id
  IS NULL`) → only the conv itself receives the message, no fan-out
  row.
- **Repeat sends don't dedupe** — two `send_to_conversation(child,
  ...)` calls produce two fan-out rows (approval may legitimately
  notify more than once).

**Sizing**: ~390 prod + ~280 tests.

---

### Step 8 — Routing accuracy eval

#### 8.1 Pre-check

```bash
grep -rn "tool_use\|trace\|tool_call" \
  src/rolemesh/evaluation/scorers/ 2>/dev/null
```

If a "read trace.tool_use args" template already exists → reuse it,
~80 lines. If not → write from scratch, ~150 lines. Record the
finding in the commit message.

#### 8.2 Dataset

`tests/data/routing_dataset.jsonl`:

- ≥ **50 cases**.
- ≥ **20% adversarial** ("looks like A, actually B").
- Each routing target has ≥ 5 cases.
- 5-10 "no-match" cases (`expected_target=null`).
- Cases that test the failure-passthrough contract: target returns
  `safety_blocked` → verify frontdesk reply contains specialist name
  + literal reason.

#### 8.3 Scorer rules

- Trace contains `delegate_to_agent` AND `target` matches `expected` → 1.
- Trace contains `delegate_to_agent` but `target` wrong → 0.
- Trace contains no `delegate_to_agent` but `expected_target` is set → 0.
- `expected_target=null` AND trace has no `delegate_to_agent` → 1.

#### 8.4 Nightly

Wire into the existing rolemesh-eval nightly job. This scorer is
**release-blocking**: any change to frontdesk system prompt, new
domain agent added, `routing_description` edited, or model swap must
pass the eval before merging.

**Sizing**: ~80-150 prod + ~70 tests + 50-cases dataset.

---

### Step 9 — Documentation

Update `docs/frontdesk-architecture.md` to v1.2. Cover:

- Motivation, architecture (parent/child ASCII diagram).
- Data model: `parent_conversation_id` column, `internal` binding,
  `delegations` table. **Explicit explanation of why we don't reuse
  the parent's `conversation_id`** (avoid sessions overwrites; the
  full route A vs B trade-off).
- Tool contracts: `list_agents`, `delegate_to_agent`.
- Synchronous + parallel semantics.
- Safety / approval / OIDC. **Async approval explicitly called out.**
- **Approval UI parent-walk + outcome fan-out (both paths)**.

The §9 known trade-off list goes in this doc.

Also update `README.md` Features section with one line for frontdesk.

**Sizing**: ~500 lines of markdown.

---

## 7. Reference data

### 7.1 NATS subjects

| Subject | Direction |
|---|---|
| `agent.{job_id}.list_agents.request` | agent → orchestrator |
| `agent.{job_id}.delegate.request` | agent → orchestrator |

### 7.2 `delegate.request` payload

```json
{
  "type": "delegate_to_agent",
  "tenantId": "<uuid>",
  "fromCoworkerId": "<uuid>",
  "fromConversationId": "<uuid>",
  "userId": "<uuid or null>",
  "target": "trading",
  "prompt": "<self-contained>",
  "contextMode": "isolated",
  "depth": 0
}
```

### 7.3 `delegate.response` payload

```json
{
  "status": "success",
  "text": "<final reply>",
  "metadata": {
    "targetCoworkerId": "<uuid>",
    "targetFolder": "trading",
    "childConversationId": "<uuid>",
    "newSessionId": "<sid or null>",
    "durationMs": 4321,
    "safetyStage": null,
    "delegationId": "<uuid>"
  },
  "isError": false
}
```

---

## 8. Common pitfalls (must avoid)

1. Do NOT add child convs to `_state.coworkers`.
2. Do NOT use JetStream for delegation RPC; use core NATS request-reply.
3. Do NOT open a new NATS connection in the agent runner.
4. Do NOT pass the frontdesk's `permissions` to the target's
   `AgentInput`.
5. Do NOT let `send_message` succeed inside a delegated call.
6. Do NOT validate `agent_delegate` only on the agent side; check on
   the orchestrator side too.
7. Do NOT add special handling for parallel delegations — the
   `queue_key` per child_conv makes them naturally concurrent.
8. Do NOT add a frontdesk-specific permission template.
9. Do NOT add a third value to the `agent_role` enum.
10. Do NOT refresh the catalog mid-turn (Step 6 is spawn-time only;
    `list_agents` tool is the in-turn refresh path).
11. Do NOT PII-filter or rewrite the prompt.
12. Do NOT assume `submit_proposal` blocks the target turn.
13. Do NOT let the audit terminal state be overwritten.
    `update_delegation_terminal` must use `WHERE status='running'`
    and return a bool.
14. Do NOT expect `asyncio.wait_for` to kill the container.
15. Do NOT read `role_config` from `Coworker` or the `coworkers`
    table — it doesn't exist there.
16. Do NOT register NATS subscriptions before
    `cleanup_running_delegations()` finishes.
17. Do NOT create a `delegated_sessions` table.
18. Do NOT forget the **explicit** `set_session(child_conv.id, ...)`
    call.
19. Do NOT add new methods to `GroupQueue`.
20. Do NOT hedge the `channel_bindings` UNIQUE constraint.
21. Do NOT let `create_child_conversation` use a plain
    SELECT-then-INSERT in sticky mode; it MUST use ON CONFLICT.
22. Do NOT just look at the `is_final=True` marker for `result`;
    Pi's marker has `result=None`. Merge with the text event.
23. Do NOT forget the explicit `queue.request_shutdown(queue_key)`
    call. Target containers do not exit naturally.
24. Do NOT call `find_child_conversation` without `channel_chat_id`.
25. Do NOT forget to fix `_coworker_from_state`.
26. Do NOT use the word "folder" in the catalog or in FRONTDESK_RULES.
27. Do NOT let `_on_output` raise without a try/except.
28. Do NOT change `MAX_DELEGATION_DEPTH` to 2. It is strictly 1.
29. Do NOT proceed to `enqueue_task` after detecting
    `_shutting_down=True`. Early return and write the audit row.
30. Do NOT stack OUTER_GUARD_S on top of the business deadline. They
    cover orthogonal failure modes (closure-never-ran vs slow LLM)
    and must produce distinct audit messages.
31. Do NOT forget the approval outcome fan-out. Users not seeing
    approval results is a blocking UX bug, not a known limitation.
32. Do NOT use `is_isolated_run: bool` for the child-conv mode
    parameter. Use `mode: ChildConvMode = Literal["sticky",
    "isolated"]` to match the tool's `context_mode`.
33. Do NOT make `ToolContext.role_config` None-able. Normalize None
    → `{}` at the construction site so downstream tool code never has
    to None-check.
34. Do NOT wrap each approval `send_to_conversation` call site for
    fan-out. Fix it once at the channel adapter layer.
35. Do NOT stub tools to test `role_config` isolation. Intercept the
    real `AgentInitData` over NATS — more robust against handler
    refactors.

---

## 9. Known v1 trade-offs (must document)

These go in `docs/frontdesk-architecture.md` and are explicit
non-goals — not bugs.

1. **Catalog static injection**: new/removed domain agents take effect
   only on next idle restart of frontdesk containers. The
   `list_agents` tool mitigates within a turn.
2. **Business deadline 300s covers slow LLMs only**, not approval
   queues (which are async).
3. **Target's internal progress is invisible to the user**: events
   from the child conv (internal binding) are dropped by WebUI. v1.5
   adds child-chip visualization.
4. **PII passes through verbatim**. No filtering.
5. **Frontdesk inherits full super_agent permissions**. "Should only
   route" is enforced by system prompt + eval, not permissions.
6. **`CONTAINER_TIMEOUT` defaults to 30 minutes IDLE timer**, not
   wall-clock. A pathological container that stays busy is not killed
   by this.
7. **Multiple orchestrator replicas not supported**: `_state.coworkers`
   is in-process state.
8. **Delegation depth strictly 1**. Domain agents default to
   `agent_delegate=False`.
9. **During a delegation, frontdesk is in an active turn**. New user
   messages queue (SDK-native behavior). Slow paths may delay next
   user message by 30-60s.
10. **Sticky session persistence is best-effort**: if `set_session`
    fails (DB blip), audit still records success (target really did
    succeed) but the next sticky call starts fresh. Log line has a
    `sticky session_id save failed` warning for ops.
11. **`prompt_sha256` is audit-dedup only, not a PII shield**. Short
    prompts SHA-256 to ~identifiable hashes.
12. **Child conv rows accumulate**. RLS isolates them and UI hides
    them, but storage grows. Archival is a follow-on.

---

## 10. Out of v1 scope

- Handoff mode
- A2A protocol adapter
- Domain-agent ↔ domain-agent calls
- Target's internal progress streamed up to frontdesk LLM or user UI
  (v1.5)
- Frontdesk-specific permission template
- Admin UI for browsing the `delegations` audit table (v1 uses SQL)
- Multi-orchestrator-replica support
- Catalog hot-reload
- Auto-retry on `safety_blocked` with a different target
- Long-approval blocking / async-notification redesign
- `docker stop` to forcibly kill a timed-out container
- A parent-conv state machine ("switch to trading direct chat")
- Child-chip visualization (v1.5)
- `wrap_on_output_with_session_save` generalization (v1.5 backlog)
- `role_config` typed accessor / namespacing (v1.5 backlog — kill the
  "dict-for-multiple-purposes" namespace debt)
- HMAC over `prompt_sha256`
- Adaptive capacity monitoring

---

## 11. When to stop and ask

- A design choice is not covered by this handbook → stop and ask.
- Existing code conflicts with what the handbook describes → stop and
  report.
- Grep finds unexpected callers not listed in the audit instructions
  → stop and report. Do not change the design on a hunch.
- mypy or ruff errors you can't resolve → stop and ask. **Do not add
  `# type: ignore` or `# noqa`** to push through.
- Existing tests fail because of your change → stop and report. **Do
  not modify the tests to fit the code.**
- A contract that pushes work onto the LLM but has no hard eval gate
  → stop and ask.
- Any "looks like path / cmd / proc" terminology in the catalog or
  prompt template that isn't listed in this handbook → stop and ask.
