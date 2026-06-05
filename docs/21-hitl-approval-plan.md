# HITL Tool Approval — Implementation Plan & Frozen Contract

> **Read this whole file before starting any session.** It is the shared
> reference for every Claude Code session that implements this feature. The
> frozen contract in §3–§5 is the hard interface that lets sessions proceed
> independently — do not drift from it without updating this file first.

## 0. Background — why greenfield, why block-and-await

A previous human-approval subsystem existed (`feat/approval` → `feat/self-approval`
v6.1) and was **deleted in full** on 2026-05-31 (PR #35, `feat/remove-approval`,
~25.8k lines). It was removed for two reasons:

1. **Too heavy** — multi-approver fallback, separation-of-duties, a worker +
   executor that re-POSTed actions to the credential proxy, action-replay
   idempotency, an audit-log trigger.
2. **Detached from the agent ReAct loop** — the hook returned `block=True`
   *immediately*, ended the turn, and an out-of-band worker re-executed the
   action later. The agent never saw the tool result inside its own reasoning
   loop.

This redesign fixes the root cause. The hook **blocks in place** (`await`s the
decision for up to `APPROVAL_TIMEOUT`); on approve it returns `None` and the
tool executes **in the same container, same turn**, so the agent receives the
real result and continues its ReAct loop normally. No worker, no executor, no
action replay.

The trade this makes: a container is **held** for up to `APPROVAL_TIMEOUT` while
waiting. That cost is paid by the idle-reaping / timeout / restart-recovery
machinery in §8 (the hard part of this feature).

**Build greenfield. Do NOT restore or copy any code from the deleted approval
subsystem** (it is in git history; ignore it). Rewriting the small pure pieces
keeps the architecture clean and free of the old model's assumptions.

## 1. Locked decisions

| Decision | Value | Rationale |
|---|---|---|
| `APPROVAL_TIMEOUT` default | **300_000 ms (5 min)** | Approver is the creator (self-approval); resolves in seconds–minutes. Short timeout cuts container-hold cost ~4× and keeps a wide margin below the container watchdog floor. |
| Safety `require_approval` boundary | **PRE_TOOL_CALL enters HITL; other stages stay a hard block** | The safety->approval bridge (§11.4) turns a PRE_TOOL_CALL `require_approval` verdict into a HITL ticket carrying `triggered_by` provenance, reusing the same block-and-await path as an MCP-policy approval. Every other stage (INPUT_PROMPT / MODEL_OUTPUT / POST_TOOL_RESULT) keeps the hard-block alias — only PRE_TOOL_CALL has both an awaiting agent and an approval surface. |

## 2. Scope redlines (locked for the whole implementation — do not expand)

- Only MCP tools (`mcp__*` prefix). Everything else returns `None` (allow) immediately.
- Tenant-level policy only — no coworker dimension on the policy.
- Structured comparison conditions only (no expression language / no eval).
- No separate audit-log table — decision data lives on the `approval_requests` row.
- Do **not** change the GroupQueue key model — reuse the existing key rule (§5).
- Do **not** modify the Pi kernel — use the existing hook bridge (§6 anchors).
- Approver = task creator (self-approval). This is a **guardrail against agent
  mistakes**, not an authorization / separation-of-duties control. Say so.

## 3. FROZEN CONTRACT — IPC (NATS subjects)

All approval traffic is relayed through the orchestrator; the container never
talks to the user directly. Three subjects. Unknown fields MUST be dropped on
receive (forward-compat for rolling upgrades).

### 3.1 `agent.{job_id}.approval_request` — container → orchestrator
```json
{
  "request_id": "uuid",
  "tenant_id": "uuid",
  "coworker_id": "uuid",
  "conversation_id": "uuid | null",
  "user_id": "uuid | null",          // approver = creator; null => fail-closed block
  "job_id": "string",
  "policy_id": "uuid | null",
  "mcp_server_name": "string",
  "tool_name": "string",
  "params": { },                      // the tool call arguments
  "action_summary": "string",         // short human-readable summary for the card
  "requested_at": "iso8601",
  "expires_at": "iso8601"             // requested_at + APPROVAL_TIMEOUT
}
```

### 3.2 `agent.{job_id}.approval_decision` — orchestrator → container
```json
{
  "request_id": "uuid",
  "decision": "approve | reject",
  "decided_by": "uuid",
  "note": "string | null"
}
```

### 3.3 `agent.{job_id}.approval_cancel` — container → orchestrator
```json
{ "request_id": "uuid" }
```
Emitted from the container's `finally` (idempotent). Covers reject / timeout /
user Stop (CancelledError) / exception — every path where the container knows
"this round is over".

## 4. FROZEN CONTRACT — DB schema

Use the **real** RLS pattern (NOT "dual-pool" — that name does not exist in this
codebase): a single predicate `tenant_id = current_tenant_id()` for all four DML
ops, with roles `rolemesh_app` (NOBYPASSRLS) / `rolemesh_system` (BYPASSRLS).
Template lives at `src/rolemesh/db/schema.py:1244` (roles) and `:1399` (the
SELECT/INSERT/UPDATE/DELETE policy tuple).

### 4.1 `approval_policies`
```
id               uuid PK
tenant_id        uuid NOT NULL
mcp_server_name  text NOT NULL
tool_name        text NOT NULL           -- exact name or "*"
condition_expr   jsonb NOT NULL          -- see §7
enabled          bool NOT NULL DEFAULT true
priority         int  NOT NULL DEFAULT 0
created_at       timestamptz NOT NULL DEFAULT now()
updated_at       timestamptz NOT NULL DEFAULT now()
-- indexes: (tenant_id, enabled), (tenant_id, mcp_server_name, tool_name)
-- RLS: tenant_id = current_tenant_id()
```

### 4.2 `approval_requests`
```
id               uuid PK
tenant_id        uuid NOT NULL
coworker_id      uuid NOT NULL
conversation_id  uuid NULL
policy_id        uuid NULL
user_id          uuid NULL               -- approver = creator; null => fail-closed
job_id           text NOT NULL
mcp_server_name  text NOT NULL
action           jsonb NOT NULL          -- { tool_name, params }
action_summary   text
status           text NOT NULL           -- pending|approved|rejected|expired|cancelled
decided_by       uuid NULL
note             text NULL
requested_at     timestamptz NOT NULL DEFAULT now()
expires_at       timestamptz NOT NULL
decided_at       timestamptz NULL
-- indexes: partial index on (status) WHERE status='pending'; (job_id)
-- RLS: tenant_id = current_tenant_id()
-- DB is authoritative; in-memory suspend state is only a cache (see §8 restart recovery).
```

No `approval_audit_log` table, no `resolved_approver_user_ids` (self-approval ⇒
approver is `user_id`), no `action_hashes` (no replay).

## 5. FROZEN CONTRACT — config & invariants

```
APPROVAL_TIMEOUT      core/config.py    300_000 ms (5 min)    container await + DB expires_at share this
startup assertion     core/config.py    APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000   else refuse to start
```
With `IDLE_TIMEOUT = 1_800_000 ms` and the container watchdog floor
`max(config_timeout, IDLE_TIMEOUT + 30_000)` (`container_executor.py:402`), the
assertion guarantees the approval await always fires before the container
watchdog — so the watchdog can never pre-empt an approval.

**Queue key rule** (reuse, do not reinvent): `conversation_id or coworker_id`.
Existing impl: `src/rolemesh/orchestration/task_scheduler.py:99-115`
(`_compute_queue_key`); messaging side keys on `conv.id` at `main.py:792`. The
container and its approval suspend state MUST land on the same `_GroupState`
entry, so use this exact rule.

## 6. Verified code anchors (already confirmed against main — trust these)

These were verified by reading the code; you do not need to re-discover them.

| Concern | Location | Note |
|---|---|---|
| Unified hook iface | `src/agent_runner/hooks/registry.py:46` `on_pre_tool_use` | async; returns `ToolCallVerdict \| None` |
| Verdict type | `src/agent_runner/hooks/events.py:35` `ToolCallVerdict(block, reason, modified_input)` | `None`=allow; `block=True`=deny |
| Claude wiring | `claude_backend.py:446` permission_mode=bypassPermissions; `:448` hooks; `:171` callback | hooks fire regardless of permission_mode |
| Pi bridge | `pi_backend.py:142-175` `handle_tool_call`; `:234` registration | block verdict prevents execution; `modified_input` is dropped at `:166` (we don't need it) |
| Idle timer | `main.py:845-855` `_reset_idle_timer` (TimerHandle); `core/config.py:43` IDLE_TIMEOUT=1_800_000 | |
| Progress early-return | `main.py:868-882` ("Don't touch idle timer or notify_idle") | status heartbeats do NOT reset idle — must use explicit suspend |
| Reaping path A | `main.py:854` idle timer → `request_shutdown` | standalone TimerHandle, not gated on idle_waiting |
| Reaping path B | `scheduler.py:262-267` `notify_idle` → if pending | gated on `idle_waiting` |
| Reaping path C | `scheduler.py:196-205` `enqueue_task` → active && idle_waiting | gated on `idle_waiting` (line 202) |
| `idle_waiting` flag | `_GroupState` `scheduler.py:44`; set False at `:274/:379/:414` | |
| Container watchdog | `container_executor.py:399-402` `max(config_timeout, IDLE_TIMEOUT+30_000)` | |
| Sessions / resume | `db/schema.py:690` table; `db/chat.py:483-510` get/set_session by conversation_id | enables "continue → retry tool → re-hit hook → new approval" |
| RLS template | `db/schema.py:1244` roles, `:1399` policy tuple | single-predicate tenant pattern |
| NATS input | `agent_runner/main.py:371` KV "agent-init" (initial); `:373` `agent.{job_id}.input` sub; `:405-407` immediate ack | no custom ack_wait → no redelivery during a block. **Do NOT set ack_wait < APPROVAL_TIMEOUT on any sub the blocked container relies on.** |
| v1 WS frame | `webui/schemas_v1.py:835-859` `WsClientFrameModel` union; `webui/v1/ws_stream.py` dispatch | extension = new pydantic member + union + ws_stream branch + publish NATS + OpenAPI regen |
| Telegram | `channels/telegram_gateway.py` (NO inline keyboard / CallbackQueryHandler yet — build fresh); `db/chat.py:138` binding-for-bot-token | callback_data "apr:{uuid}"/"rej:{uuid}" ≈ 40B, within Telegram's 64B limit |

### Verified concurrency model (important for §8)
- `HookRegistry.emit_pre_tool_use` iterates handlers **serially** (`registry.py`).
  We register a single approval handler, so that's fine.
- BUT multiple `ToolUseBlock`s in one turn are dispatched **concurrently** by
  both backends (Claude SDK parallel tool calls; Pi `asyncio.gather` over the
  batch). So **multiple approvals can be pending simultaneously in one turn** →
  the suspend state MUST be a `set[request_id]`, never a bool.

## 7. Policy condition language (pure function, fail-closed)

`evaluate_condition(expr: dict, params: dict) -> bool` in
`src/agent_runner/approval/policy.py`. Zero external deps. Used by both the
container hook and the orchestrator.

```
{"always": true}
{"field": "amount", "op": ">", "value": 100}
{"and": [ ... ]}    {"or": [ ... ]}
```
Ops: `== != > >= < <= in not_in contains`.

**Fail-closed**: missing field / type mismatch / malformed expr / any exception
⇒ return such that approval IS required (i.e. treat as match). A policy gate that
can't evaluate must err toward asking the human.

Matching (`find_matching_policy`): pull all `enabled` policies for the tenant;
match `mcp_server_name` AND (`tool_name == "*"` OR exact) AND condition true;
multiple matches → highest `priority`, tie → newest `updated_at`.

## 8. The hard part — idle suspend / resume / restart recovery

Status heartbeats do NOT work (progress output early-returns at `main.py:873`
without touching the idle timer). A legitimate, bounded ≤5-min approval wait must
**explicitly suspend** reaping, not fake liveness.

There are **three** reaping paths (§6 A/B/C). Suspend must close all three.

### Suspend (orchestrator receives `approval_request`)
1. Persist row `pending` + `expires_at`.
2. `idle_handle.cancel()` (closes path A).
3. Force `state.idle_waiting = False` + assert (closes paths B/C — do not rely on
   implicit invariants).
4. `awaiting_approval[key].add(request_id)` — **`set`, not bool** (concurrent
   multi-approval; clearing on the first decision while a second is still pending
   would re-arm a full IDLE_TIMEOUT and mis-kill the second).
5. Send one "⏳ waiting for approval" status to the UI (alongside the card).
6. While suspended: no path (including a new follow-up message) may re-arm idle;
   new messages still enqueue but must not reset the timer.

`key` = the §5 queue key. `awaiting_approval` is a shared dict keyed by it; do
NOT refactor the GroupQueue key model to add it.

### Resume (orchestrator receives `approval_decision` OR `approval_cancel`)
1. `awaiting_approval[key].discard(request_id)`.
2. **Iff the set is now empty** → re-arm one full `IDLE_TIMEOUT` (from now).
3. If a decision: forward it to the container (`approval_decision` subject); the
   hook unblocks and the normal flow resumes.

### Expiry watcher
Container-SIGKILL fallback (the container's `finally` never runs): the
orchestrator expires the row at `expires_at` and fires the hard-channel
notification.

### Restart recovery (MUST be complete — `_groups` is in-memory and lost on restart)
Containers in approval-wait **survive** an orchestrator restart, so recovery is
NOT just reloading rows. On startup, scan `approval_requests WHERE status='pending'`
and for each row:
- **Not expired** → reconstruct the FULL suspend state for that live container:
  rebuild its `_GroupState` entry, **replay the suspend actions** (cancel
  idle_handle, force `idle_waiting=False`, `awaiting_approval[key].add`),
  re-establish the `approval_decision` routing (subject derivable from `job_id`),
  and re-create the expiry watcher. If you only reload the row but skip rebuilding
  `_GroupState` + suspend, the recovered container gets reaped immediately.
- **Expired** → mark `expired` + fire hard-channel notification.

### Three-layer cleanup (orthogonal, graceful degradation)
1. Normal: `approval_decision` (approve/reject).
2. Container-end fallback: deterministic `approval_cancel` from the container's
   `finally` (covers Stop / abort / exception / timeout).
3. Container-SIGKILL fallback (finally didn't run): orchestrator expiry watcher +
   restart recovery.

### Decision race / idempotency
Late click vs timeout-then-approve: container Future is first-wins; orchestrator
row-level `status` transition is idempotent. Both sides converge.

## 9. Known risks tracked across sessions

- **R1 (S2, MUST be answered in MVP):** after approval, the tool executes against
  an MCP connection / credential-proxy token that has been idle through a ≤5-min
  block. The deleted worker re-fetched creds at execution time; this model does
  not. Verify the MCP stdio/http connection and cred-proxy token survive the
  block; define and surface "tool failed after approval" behavior to the user.
  This is the only functional regression vs the old model — close it.
- **R2 (S3):** restart recovery must rebuild `_GroupState` + replay suspend for
  live containers, not just reload DB rows (see §8).
- **R3 (resolved):** timeout default = 5 min (§1).
- **R4 (revised — safety->approval bridge):** a PRE_TOOL_CALL safety
  `require_approval` verdict now enters HITL via the bridge (§11.4), carrying
  `triggered_by` provenance; other stages stay a hard block (§1).
- **Operational:** each pending approval holds one container ≤ APPROVAL_TIMEOUT.
  Confirm `MAX_CONCURRENT_CONTAINERS` / `GLOBAL_MAX_CONTAINERS` headroom; accepted
  trade-off, but `log()` it if a cap is hit.
- **Known upstream (test note, not introduced here):** Pi warm-idle follow-up
  delivery has a pre-existing quirk (messages stuck in `pending_messages` instead
  of via `agent.{job_id}.input`). Treat as a known quantity when testing
  "new message during suspend".

## 10. Session plan

**Branch:** all sessions work on **`feat/hitl-approval-B`** (single shared branch).
Do NOT open a PR or merge to `main` per session — commit incrementally on this
branch and **merge to `main` only once the whole feature (S1–S5) is complete**.

Each session: read this file first; `git checkout feat/hitl-approval-B`; commit
incrementally with `git commit -s` (no Co-Authored-By); ship tests with the code.
Because sessions share one branch, the first thing each later session does is
`git log --oneline` to see what prior sessions landed, and it builds on §3–§5's
frozen contract rather than re-deriving interfaces. Adversarial tests per the
project testing philosophy — find real bugs, do not write mirror tests, minimize
mocks.

```
S1 (foundation + frozen contract)
   ├──> S2 (container blocking hook) ┐
   └──> S3 (orchestrator suspend/resume) ┘──> S4 (delivery + notify + E2E = MVP) ──> S5 (policy CRUD + isolation + docs)
```
Sequential critical path: **S1 → S2 → S3 → S4 → S5** (5 sessions). S2 and S3 may
run in parallel once S1 freezes the contract (4 waves). **S3 is the highest risk
and may span two sessions (S3 → S3-cont); do not push unfinished race work into S4.**
MVP = S1–S4.

### S1 — Foundation + freeze contract — risk: low
- Tables `approval_policies` / `approval_requests` + RLS (real single-predicate
  pattern, §4). `db/approval.py` CRUD via tenant_conn / admin_conn.
- `agent_runner/approval/policy.py`: `evaluate_condition` + `find_matching_policy`,
  pure, fail-closed (§7).
- Config: `APPROVAL_TIMEOUT=300_000` + startup assertion (§5).
- **Confirm/lock the §3 IPC field schema** as the contract for S2/S3.
- Tests: condition edge cases (empty params, missing field, type mismatch, nested
  and/or, `always`), fail-closed ⇒ requires approval, priority/updated_at tiebreak,
  cross-tenant RLS read/write isolation.
- **Exit:** pure-function + RLS tests green; contract confirmed.

### S2 — Container blocking hook + IPC — risk: medium
- Approval hook handler: non-`mcp__*` → allow; match → publish `approval_request`
  → `await` decision Future (bounded by APPROVAL_TIMEOUT) → approve ⇒ `None`,
  reject/timeout ⇒ `ToolCallVerdict(block=True, reason=...)`.
- `request_id → asyncio.Future` map; subscribe `approval_decision` at backend
  `start()`; route decisions back to await points (support concurrent
  multi-approval in one turn).
- `finally` → idempotent `approval_cancel` on every exit path
  (reject/timeout/Stop/exception).
- Load policy snapshot at container init.
- **R1:** empirically check MCP connection + cred-proxy token validity after a
  multi-minute block; define "tool failed after approval" UX; write up the result.
- Tests: allow/block by policy; timeout→block; Future routing; concurrent double
  approval routed independently; `finally` emits cancel on all four exit paths;
  non-mcp prefix allowed.
- **Exit:** single-container approval loop runs against a stub orchestrator;
  R1 finding recorded.

### S3 — Orchestrator suspend/resume + sweep + restart recovery — risk: HIGH
- Suspend / resume exactly per §8 (set-based, force idle_waiting=False + assert,
  re-arm only when set empties).
- `awaiting_approval` shared dict keyed by §5 queue key; no GroupQueue refactor.
- Expiry watcher; decision race idempotency (Future first-wins + row-level status).
- **R2:** full restart recovery — reload pending ∪ rebuild `_GroupState` ∪ replay
  suspend ∪ re-establish decision routing; expired → mark + hard notify.
- Tests (timer-lifecycle focused): suspend→re-arm→normal teardown;
  suspend→container-timeout→teardown; concurrent double approval → only last
  re-arms; restart recovery → live container re-adopted, not reaped; no
  double-cancel / mis-clear.
- **Exit:** all timer-lifecycle + restart-recovery tests green. If not green by
  end of session, continue in S3-cont — do not proceed to S4.

### S4 — Delivery + dual-channel notify + E2E (MVP) — risk: medium
- Target resolution: `conversation_id → channel_bindings → channel_chat_id`;
  scheduled-task with no active conversation → fall back to most recent.
- Telegram: inline ✅/❌ card + `CallbackQueryHandler`, `callback_data`
  `apr:{request_id}` / `rej:{request_id}`. **Build fresh** (no inline kb on main).
  **IDOR guard:** approver identity resolved from the auth handshake (ticket + DB),
  never trusted from client payload.
- Web: new v1 WS client frame (pydantic member + `WsClientFrameModel` union +
  `ws_stream` receive branch + publish NATS + OpenAPI regen + ts client) + push
  approval event; persist scheduled-task web notifications (survive disconnect).
- Dual-channel result: soft (block `reason` → agent context) + hard (orchestrator
  deterministically edits the card to "❌ rejected" / "⏰ expired", no LLM).
- **R4 (revised):** PRE_TOOL_CALL safety `require_approval` is bridged into HITL
  (§11.4); other stages stay a hard block.
- Verify: end-to-end `amount > 100` self-approval on **both** Telegram and Web
  (approve → agent gets result and continues; reject → agent gets block reason +
  user gets hard-channel card); resume ("continue" → retry tool → re-hit hook →
  new approval).
- **Exit:** MVP end-to-end works on both channels.

### S5 — Policy CRUD + isolation hardening + docs — risk: low
- Policy CRUD REST + Web UI condition-builder form.
- attack-sim cross-tenant isolation: tenant A cannot see / decide tenant B's
  approvals (RLS + IDOR).
- Finalize this doc + a `-cn.md` translation (keep English terms per repo
  convention); record the block-and-await vs old block-and-replay difference, the
  R1 finding, and the R2 recovery semantics.
- **Exit:** cross-tenant isolation tests green; docs complete.

## 11. Implementation outcomes (filled in as sessions landed)

The feature shipped S1–S5 on `feat/hitl-approval-B`. This section consolidates
the cross-session findings the plan asked each session to record, so the doc is
self-contained for review and the eventual `main` merge.

### 11.1 block-and-await vs the deleted block-and-replay (the core difference)

| Aspect | Deleted v6.1 (block-and-replay) | This redesign (block-and-await) |
|---|---|---|
| Hook return on a gated call | `block=True` **immediately**; the agent's turn ends | `await`s the decision in place (≤ `APPROVAL_TIMEOUT`) |
| Where the tool runs on approve | An out-of-band worker/executor **re-POSTs** the action later | The **same container, same turn** runs it; the agent gets the real result in its ReAct loop |
| Agent's view of the result | Never sees it inside its own reasoning | Sees it inline and continues normally |
| Moving parts | worker + executor + action-replay idempotency + audit-log trigger | none of these — one hook that blocks, one orchestrator coordinator |
| Cost paid | none (turn ended) | a container is **held** ≤ `APPROVAL_TIMEOUT`; covered by the §8 suspend/reap machinery |

The trade is deliberate: holding a container is the price of giving the agent a
truthful, in-loop tool result instead of a detached replay. The §8 suspend /
expiry / restart-recovery machinery is what makes that hold safe.

### 11.2 R1 finding — does the post-approval tool call survive the block? (resolved, S2)

**Conclusion: yes, in-process; one residual edge is environment-specific.**

- The block is **cooperative** — the hook `await`s an `asyncio.Future`; the
  event loop is never frozen, so MCP keepalives, NATS decision delivery, and
  the idle/interrupt pollers keep ticking during the wait.
- MCP connection lifecycle is **container/turn-scoped**, not per-call (Claude
  registers `mcp_servers` for the whole `run_prompt`; Pi reuses
  `McpServerConnection`s for the container lifetime). **Nothing in our code
  closes an MCP connection during a block.**
- **No container-held credential token ages out**: LLM creds are injected
  per-request by the credential proxy (the container holds only
  `ANTHROPIC_BASE_URL`, not a bearer); external MCP auth uses the static
  per-request `X-RoleMesh-User-Id` header. There is no in-container token whose
  validity lapses across a 5-min wait. (This is the one regression vs the old
  model — which re-fetched creds at execution time — and it is closed.)
- **Residual (not closeable by a unit test):** a *remote* MCP server or an
  intermediary may drop an idle HTTP/SSE session during the block. The 5-min
  timeout keeps the window short; mitigations if it proves real in staging:
  lower `APPROVAL_TIMEOUT`, rely on transparent MCP client reconnect, or a
  keepalive ping to gated servers. None are needed for correctness.
- **"Tool failed after approval" UX:** there is no separate "approved-but-
  failed" hard channel. A failed post-approval call surfaces through the
  **normal tool-error path** (Claude `PostToolUseFailure`; Pi `tool_result`
  with `is_error`) — the agent sees the error in-context and reports/retries.
  A retry that re-hits the hook produces a **new** approval request.

### 11.3 R2 recovery semantics — surviving an orchestrator restart (resolved, S3)

`_groups` is in-memory and lost on restart, but an approval-held container
**survives** the orchestrator, so recovery is more than reloading rows. On
startup, `recover_pending()` scans `approval_requests WHERE status='pending'`
(cross-tenant, via `admin_conn`) and for each row:

- **Not expired** → `adopt_orphan_container` rebuilds a minimal active
  `_GroupState`, **replays the suspend actions** (cancel idle handle, force
  `idle_waiting=False`, `awaiting_approval[key].add`), restores the
  `approval_decision` route (subject derived from the row's `job_id`), and
  re-arms the expiry watcher. Without rebuilding `_GroupState` the re-adopted
  container would be reaped immediately.
- **Expired** → mark `expired` + fire the hard-channel notification.
- The whole pass is **idempotent** across a double run.

**Known ops caveat:** the default Docker runtime force-removes `rolemesh-`
containers via `cleanup_orphans` in `_ensure_container_system_running` *before*
recovery runs, so in that deployment the container to re-adopt is already gone.
`recover_pending()` **degrades safely** there — the expiry watcher fires, the
row is marked `expired`, and `_reap_adopted` clears the rebuilt state so the
conversation is not wedged. The re-adoption path is correct for runtimes/configs
that keep the container alive across a restart. (Tracked as an ops item, not an
MVP blocker.)

### 11.4 R4 (revised): safety->approval bridge at PRE_TOOL_CALL

The safety pipeline and the HITL approval system are connected at the one stage
where it is meaningful: **PRE_TOOL_CALL**. There an agent is blocked waiting on a
tool call (an approval surface) inside its own container, exactly like a business
MCP-policy match — so a `require_approval` verdict is turned into a real HITL
ticket instead of a terminal block.

How it works (the container path, `agent_runner.safety.hook_handler`):

- `pipeline_core` stamps the firing rule's provenance (`firing_rule_id` /
  `firing_check_id`) onto a short-circuit verdict.
- On a PRE_TOOL_CALL `require_approval` verdict, the handler builds a
  `triggered_by = {kind: "safety_rule", rule_id, check_id, stage}` object and
  publishes `approval_request` through the **shared `ApprovalAwaiter`** — the
  same block-and-await primitive the business hook uses (`policy_id` is null;
  the provenance rides `triggered_by`). It then blocks on the same
  `APPROVAL_TIMEOUT` bound as a business approval.
- The orchestrator persists `triggered_by` on the `approval_requests` row and
  forwards it on the `event.approval.requested` WS push and the REST projection,
  so the SPA renders the amber "paused by a safety rule" banner.
- approve → the handler returns `None` and the tool runs in-band, same turn;
  reject / timeout / cancel → a block verdict reaches the model.

The other stages stay a hard-block alias for `require_approval`: INPUT_PROMPT and
POST_TOOL_RESULT have no clean "approve then continue" semantics, and MODEL_OUTPUT
runs orchestrator-side with no awaiting container. Two gate types still coexist
(business MCP policy vs safety rule); the SPA tells them apart by the presence of
`triggered_by`.

## 12. S5 deliverables (this session)

- **Policy CRUD REST** — `GET/POST /api/v1/approval-policies`,
  `GET/PATCH/DELETE /api/v1/approval-policies/{id}` over the S1 `db/approval.py`
  helpers; strictly tenant-scoped (RLS + explicit `WHERE tenant_id`). A
  malformed `condition_expr` is rejected at the API (422) via a new pure
  `validate_condition_expr` (the strict, write-time companion to the lenient,
  fail-closed `evaluate_condition`).
- **Pending-request read for web reconnect** — `GET /api/v1/approval-requests`
  (optional `conversation_id` filter) returns only pending rows for the caller's
  tenant; the projection exposes the tool name + summary, never the raw params.
- **SPA approval card** — `rm-approval-card` renders `event.approval.requested`
  (summary + ✅/❌), relays a tap via `V1WsClient.sendApprovalDecision`
  (`request.approval_decision` frame; identity stamped server-side), and updates
  in place on `event.approval.resolved`. On (re)connect the chat panel re-renders
  in-flight cards from the REST read (the live push is fire-and-forget).
- **Policy CRUD Web UI** — `rm-approval-policies-page` (under Settings →
  Governance) with a structured §7 condition builder (`always` / a flat
  `and`/`or` of `field op value` leaves). A stored expression too complex for
  the flat builder opens read-only and is left untouched on save.
- **Cross-tenant attack-sim (the S5 exit criterion)** — REST-layer tests prove a
  tenant-A user wielding a tenant-B id gets a flat 404 on read/patch/delete (no
  write, no existence oracle), list never leaks, the pending read is
  tenant-scoped even under a foreign `conversation_id`, and a hostile body
  cannot smuggle `tenant_id`/`id`. The DB-layer RLS + WHERE belts were already
  proven in S1; this adds the HTTP layer above them.
- **Docs** — this §11/§12 finalization + a `-cn.md` mirror.
