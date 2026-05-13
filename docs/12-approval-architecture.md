# Approval Module Architecture

This document explains RoleMesh's human-in-the-loop approval module — the mechanism that lets administrators gate specific external MCP tool calls behind a review step, without modifying the permission model.

It covers why the module is policy-driven instead of permission-driven, which designs were considered and rejected, the split between the container-side hook and the orchestrator-side engine, and the concurrency / crash-recovery guarantees the state machine provides.

Target audience: developers adding new approval flows (multi-step reviews, policy templates), debugging a decision that didn't fire, integrating a new MCP server that needs approval gating, or porting the module to a different process topology.

---

## Background: Permissions Aren't Approval

RoleMesh already has a four-field `AgentPermissions` model (`data_scope`, `task_schedule`, `task_manage_others`, `agent_delegate`, see [`6-auth-architecture.md`](6-auth-architecture.md)). It decides "can this agent use this tool at all?" — a binary answer evaluated at tool registration time.

That model is silent on a different class of question: **"can this agent do this particular thing RIGHT NOW without a human eyeballing it first?"** Consider:

- Agents can call MCP tools that issue refunds, update prices, modify access grants. The ERP/CRM server itself has no authorization context; it trusts whoever the JWT says is calling.
- A tenant admin may be happy for the agent to *read* CRM records unsupervised, but want human approval on any refund > $1000.
- The risk isn't "rogue agent" (that's covered by `AgentPermissions`); it's "reasonable agent, bad judgment" — e.g. refunding 200 orders to close 200 complaints when only 3 were actually valid.

Options that failed to address this shape:

- **Hard-code approval into the MCP server.** Doesn't scale — every MCP vendor would have to implement our approval semantics.
- **Use `AgentPermissions` with finer-grained flags.** The decision is per-call, not per-tool, and depends on runtime parameters (`amount > 1000`). Boolean flags cannot express "block only when amount exceeds threshold X."
- **Hook every MCP call into a manual UI.** Breaks ergonomics — nobody wants to approve 50 read-only CRM lookups to make the agent useful.

The module we built is **policy-driven**: admins write declarative rules keyed on `(mcp_server, tool_name, condition_expr)` and a specific set of approvers. The hook system decides which calls match. When no policies exist, the module adds zero runtime cost and bit-identical agent behaviour — a property pinned by the test suite.

---

## Design Goals

1. **Zero impact when unused.** A tenant with no approval policies must observe behaviour identical to a pre-module build. `ApprovalHookHandler` is not registered, no NATS subscriptions are created for approvals at the agent level, no extra queries touch the DB on the hot path.
2. **Single-implementation policy matcher.** Container-side and orchestrator-side code import the **exact same file** (`src/agent_runner/approval/policy.py`), so there is no possibility of the hook and the engine disagreeing on "did this call match?"
3. **Decision/execution decoupled.** The REST decide endpoint returns within ~100ms after DB write + NATS publish. Actual MCP execution runs in a separate worker. A batch of 50 actions cannot tie up the approver's HTTP connection.
4. **Atomic transitions.** Two approvers clicking Approve at the same instant: exactly one wins, the other sees 409. Two workers claiming the same approved request: exactly one executes. Both are single-statement SQL, not advisory locks.
5. **Fail-close at the gate.** If the hook system crashes while checking a policy, the call MUST be blocked, not allowed. This leverages the existing hook-system fail-close contract (see [`9-hooks-architecture.md`](9-hooks-architecture.md) §"Fail-close vs fail-safe").
6. **Append-only audit.** The application layer never offers update or delete on `approval_audit_log`. Every state transition is an audit row; no row is ever rewritten.
7. **Stop button integrates.** Hitting Stop on an agent turn cancels its pending approvals so approvers don't act on abandoned work.
8. **Two agent backends, one approval code.** No backend-specific branches. The module hooks into the unified hook system.

---

## Alternatives Considered

### Option A — Enforce Approval Inside Each MCP Server

Push the approval requirement down: MCP servers (ERP, CRM, etc.) implement their own "require human sign-off" flow and return a pending-approval result to the agent. RoleMesh just surfaces it.

**Pros**

- No RoleMesh-side state. Simplest code path.
- Works even if the agent bypasses RoleMesh's hook.

**Cons**

- Every MCP vendor has to implement our approval UX and know about RoleMesh users.
- Inconsistent across servers — a home-grown ERP and a third-party CRM would notify approvers differently.
- MCP protocol has no standard for "pending, come back later" — the agent would have to poll.

**Rejected.** MCP servers should stay dumb about the deployment they run in. Approval is a RoleMesh concept.

### Option B — Approve Inside `AgentPermissions`

Add an `approval_required: dict[str, list[dict]]` field on `AgentPermissions` listing per-tool policies. Decision happens on the container side at `PreToolUse`, round-trips to a minimal orchestrator endpoint, waits inline.

**Pros**

- Unified with the existing permission model.
- No new tables.

**Cons**

- Couples two orthogonal concepts: "is the agent allowed" vs "does this instance need review". An admin toggling `task_schedule` risks touching approval semantics accidentally.
- Inline await means the agent turn blocks for minutes while the approver decides — holds a container slot idle under load.
- No batch approval. Every tool call gets its own review.

**Rejected.** The two concerns should stay separate. `AgentPermissions` stays boolean and fast; approvals are a stateful side-channel.

### Option C — Side-Channel via External Review Tool

Integrate with an existing approval platform (e.g. a ChatOps bot, a separate GRC system). Emit webhooks, wait for callback.

**Pros**

- Reuse an existing review UX.

**Cons**

- External dependency on day 1 of the feature.
- Harder to correlate "approval X is for job_id Y on coworker Z" — approval platforms don't model agent runs.
- Stop-cascade impossible: an external system doesn't know the agent turn was aborted.

**Rejected for v1.** An adapter layer could be added later (emit an event *and* create a local approval request that mirrors external state), but the core module must own the primitive.

### Option D — Policy-Driven, DB-Backed, Hook-Gated (Chosen)

- Declarative policies in Postgres, keyed on `(mcp_server, tool_name, condition_expr, priority)`.
- A `PreToolUse` hook on the container evaluates policies against the call's arguments.
- Match → block the call, publish an auto-intercept IPC to the orchestrator, which creates a pending approval row and notifies approvers.
- Approve decision → publish `approval.decided.<id>` → Worker picks up asynchronously, claims the row atomically, POSTs to the credential proxy, writes the result report back to the originating conversation.

**Pros**

- Policy is declarative and auditable.
- Approvers act on a deliberate summary (with rationale when the agent used `submit_proposal`), not on raw tool payloads.
- Decoupled execution doesn't block the decision handler.
- Zero impact when no policies exist.
- Batch approvals possible via `submit_proposal`.

**Cons**

- Non-trivial state machine (10 statuses, 3 actors for audit rows).
- Requires the container to be online to publish auto-intercept (not an issue in practice — the hook fires only while the container is running).

**Chosen.** This is the shape the rest of the document describes.

---

## Per-Tenant Defaults

A tenant row carries two settings that shape approval behavior without requiring any per-policy configuration:

### `approval_default_mode` — what happens when a proposal matches no policy

| Value | Behaviour when NO policy matches |
|---|---|
| `auto_execute` (default) | create request, immediately publish `approved`, Worker executes unsupervised. Legacy mode — preserves behavior for existing deployments. |
| `require_approval` | create request as `skipped`; Worker never sees it; origin gets a "could not proceed" message. Use when "no policy" should be treated as "config gap, not allowlist". |
| `deny` | create request as `rejected` with a system note; Worker just delivers the rejection notification. Deny-by-default posture. |

Configure via `update_tenant(tenant_id, approval_default_mode=...)` or REST `PATCH /api/admin/tenant`. The engine reads the current value on every proposal, so changes take effect immediately (no restart).

### `APPROVAL_FAIL_MODE` — DB unreachable at container startup

Env var read by `container_executor` when the policy lookup raises.

| Value | Behaviour on DB outage |
|---|---|
| `closed` (default) | re-raise the exception; the agent container does not start. A policy outage MUST NOT silently let every tool call run unsupervised. |
| `open` | start the container with no approval policies loaded; log a warning. Legacy behavior; acceptable when the tenant values agent availability over approval coverage during incidents. |

---

## Two Entry Paths

There are **two** ways a call reaches a pending approval request. Both end up in the same engine state machine.

### Path 1: Auto-intercept (PreToolUse hook)

Unsolicited. The agent decides to call `mcp__erp__refund(amount=5000)`. The hook matches a policy with `condition_expr={"field":"amount","op":">","value":1000}`, publishes an `auto_approval_request` NATS task to the orchestrator, and returns a `block` verdict whose reason explains what happened. The agent sees a tool error, usually tells the user "I'll wait for approval."

### Path 2: Proactive proposal (`submit_proposal` tool)

The agent proactively calls `mcp__rolemesh__submit_proposal` with a `rationale` and a batch of actions. This is the path to use when:

- Multiple related actions should be approved together ("refund these 10 orders for the storm disruption").
- The agent has context the approver will want (the "why").
- A policy exists but the agent wants to add human judgment even though the condition might not match.

Proposals with **no matching policy** are still audit-logged but short-circuit to executed — the audit trail must be continuous even when a proposal is unconditionally allowed.

The two paths share:

- The same engine methods (`handle_proposal`, `handle_auto_intercept`)
- The same state machine
- The same audit shape

They differ in:

- `source` column (`proposal` vs `auto_intercept`)
- Dedup behaviour (auto-intercept dedups on `action_hash` in a 5-minute window; proposals never dedup — each call is explicit)
- `created` audit `actor_user_id`: proposals record the originating user; auto-intercepts are NULL (system transition)

---

## Architecture

### Process topology

```
       ┌──────────────────────────────────────────────────┐
       │              Agent Container                     │
       │                                                  │
       │  ApprovalHookHandler  (PreToolUse, fail-close)   │
       │      matches policy → publish auto_approval_… on │
       │      agent.<job>.tasks                           │
       │                                                  │
       │  submit_proposal tool → same NATS subject with   │
       │      {"type": "submit_proposal"}                 │
       └────────────────────────┬─────────────────────────┘
                                │ NATS agent-ipc
                                ▼
       ┌──────────────────────────────────────────────────┐
       │                 Orchestrator                     │
       │                                                  │
       │  process_task_ipc → _IpcDepsImpl.on_proposal     │
       │                   → _IpcDepsImpl.on_auto_intercept
       │                                                  │
       │  ApprovalEngine                                  │
       │    handle_proposal / handle_auto_intercept       │
       │      uses policy.py (same file as container)     │
       │      resolves approvers, creates row, audits,    │
       │      notifies via ChannelSender                  │
       │                                                  │
       │    handle_decision (REST entry)                  │
       │      atomic decide → audit → publish             │
       │      approval.decided.<id>                       │
       │                                                  │
       │    cancel_for_job / expire_stale / reconcile     │
       │                                                  │
       │  ApprovalWorker                                  │
       │    (durable JetStream consumer)                  │
       │      claims approved rows atomically             │
       │      POSTs each action to credential proxy       │
       │      writes audit + result, notifies channel     │
       │                                                  │
       │  run_approval_maintenance_loop (30 s cadence)    │
       │    expire_stale_requests + reconcile_stuck       │
       └────────────────────────┬─────────────────────────┘
                                │ HTTP
                                ▼
       ┌──────────────────────────────────────────────────┐
       │           Credential Proxy / Egress Gateway      │
       │   /mcp-proxy/<server>/   forwards to upstream    │
       │   with the user's IdP token injected             │
       └──────────────────────────────────────────────────┘
```

### File layout

```
src/agent_runner/
  approval/policy.py               # pure-function policy matcher (zero deps)
  hooks/handlers/approval.py       # ApprovalHookHandler (PreToolUse)
  tools/rolemesh_tools.py          # submit_proposal tool
  tools/context.py                 # ToolContext + user_id

src/rolemesh/
  approval/
    types.py                       # dataclasses mirroring the 3 tables
    engine.py                      # ApprovalEngine
    executor.py                    # ApprovalWorker (consumer + HTTP)
    notification.py                # target resolver + message formatters
    expiry.py                      # maintenance loop entry point
  ipc/
    nats_transport.py              # approval-ipc stream
    task_handler.py                # submit_proposal / auto_approval IPC routes
  db/
    approval.py                    # CRUD on approval_policies / requests / audit
    schema.py                      # DDL for the three approval tables
  main.py                          # wire engine + worker + maintenance
  agent/container_executor.py      # load policies into AgentInitData

src/webui/
  admin.py                         # REST: policies CRUD + approvals CRUD + decide
  schemas.py                       # Pydantic request/response models
  main.py                          # attach ApprovalEngine to admin module
```

---

## State Machine

```
                 ┌──────────┐
                 │ pending  │
                 └─┬─┬──┬─┬─┬──┐
       ┌───────────┘ │  │ │ │  └──────┐
       │             │  │ │ │         │
   approved      rejected │ │         │
       │             │    │ │         │
       │             │ expired        │
       │             │    │ cancelled  │
       │             │    │ │         │
       │             │    │ │   skipped (no approver found)
       │                                    ▲
       ▼                                    │
   executing ──► executed                   │
       │     └─► execution_failed           │
       └─► execution_stale  (maintenance    │
                             loop catches   │
                             hung executing)│
                                            │
   proposal with no matching policy ────────┘
   short-circuits: pending → approved → executed
   (no approver involvement; full audit trail)
```

### Terminal statuses

| Status | Reached from | Who triggered |
|---|---|---|
| `rejected` | pending | approver (atomic SQL) |
| `expired` | pending | maintenance loop |
| `cancelled` | pending | Stop cascade |
| `skipped` | pending | engine, when `resolve_approvers` returned `[]` |
| `executed` | executing | Worker, all actions succeeded |
| `execution_failed` | executing | Worker, any action failed |
| `execution_stale` | executing | maintenance loop, 5-min grace exceeded |

### Key invariants

1. **`pending → approved | rejected` is atomic and wins once.** The SQL is `UPDATE … WHERE id = $1 AND status = 'pending' AND $user_id = ANY(resolved_approvers) RETURNING *`. Two concurrent approvers: exactly one gets a row back; the other gets None, which the engine translates into `ConflictError` → HTTP 409. An outsider gets None by the same rule; the engine disambiguates by reading the row: still `pending` → `ForbiddenError` → HTTP 403.
2. **`approved → executing` is atomic and wins once.** Same CAS pattern. If two Workers subscribe (e.g. during a rollout), only one executes.
3. **`pending → cancelled` is filtered.** `cancel_pending_approvals_for_job` only moves rows with `status = 'pending'`. An already-approved row in the same job is left alone — the user cannot un-approve by Stop.
4. **`resolved_approvers` is a snapshot.** Captured at creation time. Editing the policy's `approver_user_ids` later does not widen or narrow who can decide an already-open request.
5. **Audit is append-only.** `db/approval.py` exposes `write_approval_audit` and `list_approval_audit`. There is no update or delete in the API surface — adding either would require touching the DB module directly.

---

## The Single-Implementation Policy Matcher

A common failure mode of split-process architectures is: container code and orchestrator code "should" agree on matching semantics, but drift because they're different files maintained by different PRs. The result: the hook blocks a call that the engine then considers non-matching, or vice versa.

We eliminate that class of bug by making the container and the orchestrator **import the same file**:

```python
# src/agent_runner/hooks/handlers/approval.py (runs in container)
from agent_runner.approval.policy import compute_action_hash, find_matching_policy

# src/rolemesh/approval/engine.py (runs in orchestrator)
from agent_runner.approval.policy import (
    compute_action_hash,
    find_matching_policies_for_actions,
    find_matching_policy,
)
```

The module is zero-dependency stdlib (no DB, no NATS, no `rolemesh` package imports). It has two responsibilities:

- `evaluate_condition(expr, params) -> bool` — condition DSL evaluator.
- `find_matching_policy(policies, server, tool, params) -> dict | None` — applied by the hook (one call) and the engine (once per batch action).

One-implementation invariant is pinned by a grep-level acceptance test:

```bash
grep -r "def find_matching_policy" src/ | wc -l
# must be 1
```

The engine re-matches on its own side anyway — not because we don't trust the hook, but because the policy set may have changed between the container snapshot (loaded at job start) and the intercept (fired seconds or minutes later). The engine uses `get_enabled_policies_for_coworker` to re-read current state and drops the request if no policy matches anymore.

### Condition DSL

Declarative, JSON-serializable so it fits the `condition_expr JSONB` column cleanly:

```json
{"always": true}

{"field": "amount", "op": ">", "value": 1000}

{"and": [
  {"field": "amount", "op": ">", "value": 100},
  {"field": "currency", "op": "==", "value": "USD"}
]}

{"or": [
  {"field": "amount", "op": ">", "value": 10000},
  {"field": "priority", "op": "==", "value": "critical"}
]}
```

Supported ops: `==`, `!=`, `>`, `>=`, `<`, `<=`, `in`, `not_in`, `contains`.

Failure-mode semantics (pinned by `test_approval_policy.py`):

- Missing field → condition returns `False` (no match). Callers cannot accidentally gate on "presence of field."
- Type mismatch (e.g. `"100" > 50`) → `False`, no `TypeError` escape. Misconfigured policy fails closed for the hook layer (which itself is fail-close, so the call is still blocked — a double layer of safety).
- Unknown op / unknown expression shape → `False`.
- Empty `{"and": []}` / `{"or": []}` → `False`. Vacuous connectives are almost always a config mistake; we chose the safer interpretation over Python's `all([]) == True`.

### Why not Python `eval` / CEL / JSONPath

- `eval`: obvious injection risk.
- CEL / JSONPath: full expression languages; overkill for the shape we need. Adding a dependency for one feature forces reviewers to learn it. We can migrate if use cases outgrow the DSL — the surface is tiny and swap-replaceable.

---

## Identity and Idempotency

### The four identifiers on an approval request

Each row carries multiple IDs that look similar but mean different things. Mixing them up is a class of bug the schema deliberately pins:

| Field | Meaning | Nullable? | Used for |
|---|---|---|---|
| `policy_id` | Which rule matched (or last-ditch placeholder for no-match proposals) | No | audit / admin UI |
| `user_id` | The user whose turn the agent was executing | No | `X-RoleMesh-User-Id` to MCP, origin conversation lookup |
| `resolved_approvers[]` | Users allowed to click Approve/Reject | No (may be empty → skipped) | authorization on `POST /decide` |
| `actor_user_id` (audit row) | Who caused this specific transition | Yes (NULL for system) | forensics |

The spec text in the PR notes these rules, but the tests pin them harder. If you change a transition's `actor_user_id` from NULL to a user ID by accident, `test_auto_intercept_created_audit_has_null_actor` fails.

### Action hashes: one field, two jobs

`action_hashes[]` is a parallel array to `actions[]`. Each element is the SHA-256 of canonical JSON `{"tool": tool_name, "params": params}` with `sort_keys=True`. It does two things:

1. **MCP idempotency context.** Sent on the credential-proxy call (see "Credential Proxy Integration" below).
2. **Auto-intercept dedup.** `find_pending_request_by_action_hash` queries pending rows by tenant + action_hash + 5-minute window. Prevents the hook from creating 50 pending requests if the agent retries the blocked call in a tight loop.

Determinism across key orderings is a required invariant:

```python
a = compute_action_hash("refund", {"amount": 100, "currency": "USD"})
b = compute_action_hash("refund", {"currency": "USD", "amount": 100})
assert a == b
```

Also: explicit `None` vs missing field must not collide (`{"amount": None}` vs `{}` produce different hashes). Otherwise an agent that adds a null parameter would silently reuse a pre-existing approval.

---

## Notification Flow

Notifications are intentionally a separate concern from state transitions. `ApprovalEngine` calls `channel_sender.send_to_conversation` through an injected `ChannelSender` protocol; it does not know about channel gateways, Telegram, Slack, or the WebUI.

The only two processes that currently have gateway handles are:

- The orchestrator process: uses a real `_OrchestratorChannelSender` that maps `conversation_id → (binding_id, chat_id)` via the `conversations` table and the coworker state cache.
- The WebUI process: uses a no-op `_WebuiNoopChannel`. The WebUI's REST decide endpoint publishes to NATS; the orchestrator's `ApprovalWorker` receives the decided event and owns any notification the decision implies.

That's why the engine publishes `approval.decided.<id>` for **both** approved and rejected decisions (see `handle_decision`). Earlier drafts published only on approve and sent the rejection via direct `channel_sender.send_to_conversation` call; that doesn't work when the REST handler runs in a process without gateways.

### Target resolution

`NotificationTargetResolver.resolve_for_approvers` walks this chain:

1. `policy.notify_conversation_id` if set and the conversation still exists.
2. Each approver's existing conversations with this coworker — surfaces the notification in the channel where they already work.
3. Originating conversation, as a last-ditch fallback.

For v1, `cancel` / `expire` / `reject` notifications go to the originating conversation only, and send a new message rather than editing a previous one. Editing Telegram / Slack messages + pushing WebSocket state updates is future work; the spec deliberately held that back to reduce scope.

---

## Credential Proxy Integration

The Worker doesn't call MCP servers directly. It hits the credential proxy on `/mcp-proxy/<server_name>/`. The proxy injects the user's IdP token and forwards to the actual MCP server; the user identity is carried via `X-RoleMesh-User-Id` header which the proxy strips before forwarding. Full mechanics — TokenVault, `auth_mode` (`user` / `service` / `both`), and why the user-id header is stripped — live in [`6-auth-architecture.md`](6-auth-architecture.md) and [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md).

The approval-specific contract is the **idempotency key**:

```
X-Idempotency-Key: <request_id>:<action_index>
```

This is deliberately **NOT** `action_hash` (`sha256(tool, params)`). Two tenants calling the same tool with the same arguments produce byte-identical hashes, and an MCP server that honors idempotency would return tenant A's cached response to tenant B — a cross-tenant data leak. The `<request_id>:<action_index>` form is unique per approval request (UUID) and therefore per tenant and per execution. `action_hash` keeps its other role (the auto-intercept dedup key inside the engine), where the consumer is the engine itself — no cross-tenant exposure.

### Sequential, best-effort batch execution

One JSON-RPC call per action:

```json
{
  "jsonrpc": "2.0",
  "id": <i+1>,
  "method": "tools/call",
  "params": {"name": "<tool>", "arguments": <params>}
}
```

A failed action does **not** short-circuit the batch. Each action's outcome is recorded in `audit.metadata.results[i]`. Final batch status: `executed` if every action succeeded; `execution_failed` otherwise.

Per-action errors are classified into three buckets — HTTP transport failure, JSON-RPC application error (HTTP 200 with `error` set), and aiohttp exception (timeout / connection reset). Early Worker versions misclassified application errors as success; `tests/approval/e2e/test_e2e_mcp_application_error.py` pins the corrected behavior.

**Why not parallel?** MCP servers often have side effects (issuing refunds, writing to ledgers). Parallel execution loses ordering — a consumer of the audit log cannot reconstruct which side effect happened first. Most batches are 1–5 actions; serializing is rarely a latency win.

---

## Crash Recovery and Reconciliation

Three failure modes the maintenance loop handles:

### 1. Worker missed the `approval.decided.<id>` publish

The row sits in `approved` forever. Symptoms: approver clicked the button, the UI went green, but nothing ever executed. Detection: `list_stuck_approved_approvals(older_than_seconds=60)`. Remediation: republish the NATS event. The Worker's atomic claim makes double delivery safe — the first republish that lands is the only one that will transition.

### 2. Worker crashed after claiming but before completing

The row sits in `executing` forever. Detection: `list_stuck_executing_approvals(older_than_seconds=300)` — 5 minutes is the grace. Remediation: transition to `execution_stale`, send a conservative "may have partially executed" warning to the originating conversation, manual investigation required. We do NOT retry: half-done batches are dangerous (a refund that already hit the ledger would be duplicated by a blind retry).

v1 does not persist per-action progress. The notification is deliberately terse because we cannot distinguish "finished action 0, crashed on action 1" from "crashed before any action landed." If batch forensics becomes load-bearing, an `execution_progress JSONB` column on `approval_requests` and per-action append calls in the Worker is the natural extension — flagged as future work, not built speculatively.

### 3. Container crashed while holding a pending approval

The row stays `pending` until its `expires_at` deadline. The expiry loop (`engine.expire_stale_requests`) catches it, transitions to `expired`, and notifies the originating conversation. Using a CAS-guarded SQL (`expire_approval_if_pending`) ensures a concurrent decide doesn't get trampled — if the approver clicked Approve at the exact second the deadline hit, only one of the two UPDATEs wins.

### Why one combined loop, not two

Expire and reconcile run on the same cadence (30 s). Bundling them into `run_approval_maintenance_loop` means one background task, one shared DB pool acquisition cycle, and a single cancellation point at shutdown. If they diverged in cadence later, they can split.

---

## Stop Cascade

When the user clicks Stop, the backend emits `StoppedEvent` and the agent container stays alive for follow-ups (see [`11-steering-architecture.md`](11-steering-architecture.md) for the full stop semantics). Approval cascading hooks into that signal: the NATS bridge fires-and-forgets `approval.cancel_for_job.<job_id>` on every `StoppedEvent`. The orchestrator (`durable="orch-approval-cancel"` on `approval-ipc`) calls `engine.cancel_for_job(job_id)`, which:

1. Moves pending rows for that job to `cancelled` (UPDATE filtered on `status='pending'`).
2. Writes a `cancelled` audit row per cancelled request.
3. Sends a cancellation notification to each cancelled request's originating conversation.

The publish is **fire-and-forget** because:

- The container doesn't know whether the approval module is deployed. In a zero-policy tenant there are no pending rows and nobody would act on the publish; treating it as a hard dependency would couple every Stop to approval availability.
- The `approval-ipc` stream might not exist on older orchestrators. The `try/except` wrap keeps Stop working regardless.

---

## REST API

`src/webui/admin.py`:

```
GET    /api/admin/approval-policies        ?coworker_id&enabled
POST   /api/admin/approval-policies
GET    /api/admin/approval-policies/{id}
PATCH  /api/admin/approval-policies/{id}
DELETE /api/admin/approval-policies/{id}

GET    /api/admin/approvals                ?status&coworker_id
GET    /api/admin/approvals/{id}           (includes audit_log)
GET    /api/admin/approvals/{id}/audit-log
POST   /api/admin/approvals/{id}/decide    {action, note?}
```

### Status code contract

| Scenario | Code | Reason |
|---|---|---|
| Decide by authorised approver on pending | 200 | happy path |
| Decide by user not in `resolved_approvers` | 403 | `ForbiddenError` |
| Decide on already-resolved request | 409 | `ConflictError` |
| Decide on request from another tenant | 404 | no cross-tenant leakage |
| Decide when engine not wired | 503 | deployment misconfiguration |
| Decide on nonexistent request | 404 | — |

The 503 is deliberate over falling through to "success" or "500": decide is a control-plane operation, and the admin needs to know their deployment is missing the engine rather than debugging why the background worker never fires.

---

## Database Schema

### `approval_policies`

Declarative rules. Keyed on `(tenant_id, coworker_id, mcp_server_name, tool_name)`. `coworker_id = NULL` means "tenant-wide." `condition_expr JSONB` holds the DSL. Partial indexes on `enabled=TRUE` because disabled rows never participate in matching.

### `approval_requests`

Every in-flight and historical request. Key columns:

- `status TEXT CHECK (status IN (...))` — the state machine domain mirrored in `APPROVAL_STATUSES`. Changing the domain requires updating both the constraint and the Python set; a schema-sanity test compares them.
- `action_hashes TEXT[]` — parallel to `actions JSONB` array.
- `resolved_approvers UUID[]` — snapshot of who can decide.
- Five partial indexes on `(status, ...)` covering the hot lookups (pending + expired + approved + executing + per-job pending).

### `approval_audit_log`

Append-only. `actor_user_id UUID REFERENCES users(id)` nullable because system transitions have no actor. `metadata JSONB` is free-form; the Worker stuffs `{"results": [...]}` here on terminal transitions.

DDL lives in `_create_schema()` in `src/rolemesh/db/schema.py`. The approval tables are added alongside the existing ones using `CREATE TABLE IF NOT EXISTS`, so a fresh database picks them up automatically on first boot. Tenant-scoped tables are RLS-bound — see [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md).

---

## Zero-Impact Guarantee

The baseline non-approval test count (counted before the module landed) must pass unchanged after the module merges. The properties that deliver this:

1. `AgentInitData.approval_policies = None` in `container_executor.py` when `get_enabled_policies_for_coworker` returns an empty list. The field is nullable on the wire.
2. The container's `main.py` only registers `ApprovalHookHandler` when `init.approval_policies` is truthy. Empty list → no handler.
3. With no handler, the hook chain for `PreToolUse` is unchanged.
4. `submit_proposal` is in `TOOL_DEFINITIONS`, so the agent sees it in every run — but with no policies, there is no reason for the agent to call it, and invoking it with no matching policies creates an audit-only entry and proceeds.
5. The orchestrator always runs `ApprovalEngine` and `ApprovalWorker`. Both subscribe to NATS streams on startup. In a zero-policy tenant those streams receive nothing; the subscriptions are cheap.
6. The 30-s maintenance loop queries `list_expired_pending_approvals`, `list_stuck_approved_approvals`, `list_stuck_executing_approvals`. Three indexed `WHERE status = '...'` scans on an empty table. No hot-path impact.

The whole existing suite passes unchanged; that's the executable form of the guarantee.

---

## Testing Strategy

The module follows the project-wide testing philosophy (adversarial, minimal mocks, integration-first when a real dependency is cheap).

- **Pure-function matcher** (`test_approval_policy.py`) — mutation-mindset. Every boundary of every operator, every failure mode (missing field, type mismatch, empty connectives), hash determinism across key orderings, hash non-collision on explicit-None. Changing `<` to `<=` in source fails at least one test.
- **DB CRUD** (`test_db.py`, real Postgres via testcontainers). Race-safety: two approvers deciding concurrently, two workers claiming concurrently, cancel-for-job not touching approved rows. Schema sanity: `APPROVAL_STATUSES` set matches the CHECK constraint domain.
- **Engine** (`test_engine.py`, real Postgres + fake publisher/channel/resolver) — state machine end-to-end. Each audit row's `actor_user_id` is pinned by assertions, so a refactor that changes "proposal created actor" from the user to NULL (or vice versa) breaks.
- **Worker** (`test_executor.py`, real Postgres + aiohttp test server acting as credential proxy) — execution flow, partial failure, dedup under redelivery, rejected path without conversation ID.
- **REST API** (`test_api.py`, real Postgres + httpx `AsyncClient` via `ASGITransport`) — status code contract, cross-tenant 404, decide without engine → 503.
- **Abort cascade** (`test_abort_cascade.py`) — pending cancelled, approved preserved.
- **Hook handler** (`test_approval_handler.py`) — passthrough rules for non-MCP tools and builtin rolemesh tools, malformed MCP names don't crash, published NATS payload carries identity.
- **Cross-backend parity** (`test_approval_parity.py`) — the same `ApprovalHookHandler` produces identical block verdicts and NATS publishes whether wired through the Claude bridge or the Pi bridge.

All tests are colocated under `tests/approval/` (DB-backed + API) and `tests/test_agent_runner/` (container-side, no DB). `tests/ipc/` holds the IPC dispatcher route test.

---

## Known Gaps and Future Work

- **UI for approvers.** REST endpoints exist; the WebUI frontend for reviewing / deciding approvals is not implemented in this pass. Current UX: approvers get a notification in their coworker chat, click a link to `${WEBUI_BASE_URL}/approvals/<id>`, which is not yet rendered.
- **Rich policy templates.** The DSL accepts arbitrary conditions but there is no template library for common cases ("refund > $1k", "any production write"). Admins write raw JSON.
- **MCP tool parameter introspection.** There is no way for the admin UI to suggest valid field names when authoring `condition_expr`; the admin must know the MCP server's tool schema from its docs.
- **Multi-step / multi-approver workflows.** v1 is single approver per request. "Requires 2-of-3" or "chain of approvers" would need schema changes (per-approver decision table).
- **Policy hot-reload at the container level.** Policies are loaded into `AgentInitData` at container start. An edit made during a turn does not take effect until the next turn. The engine re-matches on its side using live data, so a disabled policy is respected mid-turn; but a *new* policy won't start gating until the container restarts.
- **Silent fail-closed on DB outage.** When `APPROVAL_FAIL_MODE=closed` (the default) and the orchestrator cannot load the tenant's policy snapshot at container start, the agent spawn is refused. The refusal is logged at `ERROR` level with structured fields but there is **no active push** to the tenant owner / admin — no email, no in-chat message, no metric counter, no health-check endpoint. Users see "agent not responding"; admins must be tailing logs or have external log alerts wired up to notice. Realistic failure modes: PG failover (20–30 s), app-user permission revoke, connection pool exhaustion. Low severity for self-hosted / single-team deployments; becomes operationally important for multi-tenant SaaS. If/when RoleMesh moves toward SaaS, the right fix is a dedicated health endpoint + auto-posting a "approval subsystem unhealthy" message into the tenant owner's conversation and/or bumping a Prometheus counter. Logged as a deliberate v1 omission, not a bug.
- **Stop → proposal race leaves an orphan pending row.** NATS does not guarantee cross-subject ordering, so `approval.cancel_for_job.J` can land before `submit_proposal` on `agent.J.tasks`. `cancel_for_job` finds nothing to cancel; the late proposal subsequently creates a pending row whose agent turn is already stopped. The row is reaped by the normal expiry loop (every policy has `auto_expire_minutes`, default 60 min), and approvers generally notice the stale notification. We did not add a `cancelled_jobs` tracking table because the realistic harm (an orphan request waiting up to an hour for an approver who will likely notice it is stale) was judged smaller than the cost of a second state table + a check on every create path. `tests/approval/e2e/test_e2e_race_stop_vs_proposal.py` documents the current behavior and the expiry-path reaping. Revisit if auto-approvers enter the picture.
- **Execution retry.** `execution_stale` is terminal in v1. A future iteration could add an admin "retry" endpoint that republishes `approval.decided.<id>` — safe because of the action-hash idempotency context, but only if the MCP server honors it.
- **Cancel / expire notification editing.** v1 sends new messages; editing or WebSocket state-pushing for these transitions is deferred.
- **REST pagination.** List endpoints are capped at 100; no cursor support yet.

---

## Quick Reference for Adding a Policy

```
POST /api/admin/approval-policies
Content-Type: application/json
Authorization: Bearer <admin-token>

{
  "mcp_server_name": "erp",
  "tool_name": "refund",
  "coworker_id": "<coworker-uuid>",            // or omit for tenant-wide
  "condition_expr": {
    "and": [
      {"field": "amount", "op": ">", "value": 1000},
      {"field": "currency", "op": "==", "value": "USD"}
    ]
  },
  "approver_user_ids": ["<user-uuid-1>", "<user-uuid-2>"],
  "auto_expire_minutes": 60,
  "post_exec_mode": "report",                  // v1 only accepts "report"
  "priority": 10,
  "enabled": true
}
```

The policy applies on the next agent run (policies are loaded at job start via `get_enabled_policies_for_coworker`). The orchestrator-side engine also re-reads policies on each intercept, so disabling a policy takes effect immediately for in-flight containers.

---

## Related documentation

- [`6-auth-architecture.md`](6-auth-architecture.md) — `AgentPermissions` (the "can the agent do this at all" gate), `TokenVault`, OIDC integration
- [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md) — credential proxy mechanics, `auth_mode`, the `/mcp-proxy/<server>/` route
- [`9-hooks-architecture.md`](9-hooks-architecture.md) — `PreToolUse` hook contract, fail-close discipline
- [`11-steering-architecture.md`](11-steering-architecture.md) — Stop signal that triggers the approval cancel cascade
- [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) — RLS rules on approval tables
