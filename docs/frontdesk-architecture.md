# Frontdesk Architecture — v1.5

> Status: Re-ported onto current `main`. Branch `feat/frontdesk-v2`.
> This document is the user-facing architecture overview; the re-port
> plan, decisions, and the v1 WS delegation event contract live in
> `docs/frontdesk-impl/migration-to-main.md`.
>
> **Migration deltas (re-port to main — read these before the body below,
> which still describes the original v1.2 shape):**
> - **Role gate (D1).** `main` removed the `agent_role` column. A frontdesk
>   is now `is_frontdesk=True` AND `permissions.agent_delegate=True`; a
>   delegation target is any `is_frontdesk=False` coworker. Everywhere this
>   doc says `agent_role='super_agent'`, read `is_frontdesk=True +
>   agent_delegate`; `agent_role='agent'` → `is_frontdesk=False`.
> - **Child-conv exclusion (D2).** `main` removed `conversations.requires_trigger`;
>   delegation children stay out of `_state` purely via the
>   `parent_conversation_id IS NULL` list filter.
> - **Admin surface (D4).** `main` deleted `/api/admin/*`. Frontdesk fields
>   and the approval parent-walk are on `/api/v1` (`POST`/`PATCH
>   /api/v1/coworkers` carry `is_frontdesk` / `routing_description` /
>   `permissions`; the approvals listing takes `conversation_id`). The
>   capacity-advisory helper was dropped (unwired dead code).
> - **v1.5 UX.** Child-chip progress stream (delegation sub-chips on the v1
>   chat-panel; events ride `event.delegation.*` — see the migration doc
>   Appendix A) and register-time tool gating (`delegate_to_agent` /
>   `list_agents` registered only when `agent_delegate` is set).

## What it is

The **Frontdesk** is a coworker shape that serves as the single
user-facing entry point for a tenant. Users only ever talk to the
frontdesk. The frontdesk either answers simple questions itself, or
**synchronously** delegates a turn to a domain specialist (accounting /
portfolio / trading / ...) and synthesizes the final reply. Specialists
are invisible to the user.

A tenant may have multiple frontdesks (different teams / personas) but
each user-facing conversation binds to exactly one of them. Delegation
depth is **strictly 1** — `frontdesk → specialist`, no chains.

This is an RPC pattern, not a handoff. The user's chat continues to
target the frontdesk; the specialist's conversation lives in a separate
internal child conversation that the WebUI does not subscribe to.

```
   ┌──────┐  user message            ┌────────────┐
   │ User ├────────────────────────► │ Frontdesk  │  (parent_conv,
   └──────┘                          │  (super_   │   web binding)
       ▲                             │   agent)   │
       │  final reply  (synthesized) └─────┬──────┘
       │                                   │
       │                          delegate_to_agent
       │                                   │  NATS request-reply
       │                                   ▼
       │                          ┌────────────────┐
       │  approval fan-out        │   Specialist   │  (child_conv,
       └──────────────────────────│   (e.g.        │   internal binding,
                                  │    trading)    │   parent_conversation_id
                                  └────────────────┘   = parent_conv.id)
```

## Why route B (sub-conversation), not route A (reuse parent's id)

RoleMesh's core invariant is **one conversation binds to exactly one
coworker**:

- `sessions.conversation_id` is the PRIMARY KEY (single-column),
  so a `(conversation_id, coworker_id)` resume map cannot exist.
- The orchestrator dispatches per `conversation_id` in
  `_message_loop`.
- Approval, safety, audit, and trigger gating all attribute events
  to `conversation_id`.

If the specialist were to run inside the parent's `conversation_id`
(route A), every one of those subsystems would need a defensive
`if delegated_call: ...` patch to disambiguate which coworker actually
emitted a given event. That's a tax paid by every future feature.

**Route B**: each delegation creates a new `conversation` row for the
`(parent_conv, target_coworker)` pair, linked via
`parent_conversation_id`. The two conversations have independent
`sessions` rows; the specialist's events stay attributed to the child
row; the parent's session/state is untouched. The cost is one new
column, one `internal` channel type, one `delegations` audit table, and
an `include_children=False` default on list queries — all one-time
investments.

## Data model

### `conversations` — new column

```sql
ALTER TABLE conversations
  ADD COLUMN parent_conversation_id UUID NULL
    REFERENCES conversations(id) ON DELETE CASCADE;

CREATE INDEX conversations_by_parent
  ON conversations(parent_conversation_id)
  WHERE parent_conversation_id IS NOT NULL;
```

`NULL` means "top-level user conversation"; non-`NULL` means
"delegation child of conv X". List queries default to
`include_children=False` so child conversations never show up in the
user's conversation sidebar.

### `coworkers` — two new columns

```sql
ALTER TABLE coworkers ADD COLUMN is_frontdesk BOOLEAN DEFAULT FALSE;
ALTER TABLE coworkers ADD COLUMN routing_description TEXT;
```

`is_frontdesk=TRUE` is only valid alongside `permissions.agent_delegate=True`
(migration D1 — `agent_role` was removed on `main`). The v1 API
(`webui.v1.coworkers._validate_frontdesk_role`) enforces the invariant on
create and on the effective post-update values for patch; there is no DB
CHECK constraint because future operator workflows (e.g. demote-then-flip)
need the flexibility. `routing_description` is a domain-agent capability
card read by the frontdesk LLM when routing (max 500 chars).

### `delegations` — new audit table

```sql
CREATE TABLE delegations (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                UUID NOT NULL REFERENCES tenants(id),
  parent_conversation_id   UUID NOT NULL REFERENCES conversations(id),
  child_conversation_id    UUID NOT NULL REFERENCES conversations(id),
  from_coworker_id         UUID NOT NULL REFERENCES coworkers(id),
  target_coworker_id       UUID NOT NULL REFERENCES coworkers(id),
  user_id                  UUID,
  prompt_sha256            TEXT NOT NULL,  -- audit-dedup ONLY; NOT a PII shield
  context_mode             TEXT NOT NULL,  -- 'isolated' | 'sticky'
  status                   TEXT NOT NULL,  -- 'running' | 'success' | 'error' | ...
  error_message            TEXT,
  duration_ms              INT,
  started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at                 TIMESTAMPTZ
);
```

RLS isolated. Terminal updates use conditional `UPDATE ... WHERE
status='running'` so late events cannot overwrite a terminal row —
critical when the Pi backend can interleave hook results after a
business-deadline timeout has already been recorded.

### Channel binding format

Child conversations attach to an `internal` channel binding (one per
target coworker per tenant, created idempotently via the existing
`channel_bindings.UNIQUE (coworker_id, channel_type)` constraint).
`channel_chat_id`:

- **sticky**: `internal:{parent_conv_id}:{target_coworker_id}` —
  fixed, so repeat sticky calls hit the same child conv.
- **isolated**: `internal:{parent_conv_id}:{target_coworker_id}:{uuid4()}`
  — UUID suffix prevents collisions across one-shot calls.

The child lookup MUST match on the full `channel_chat_id` — a
`(tenant, parent, coworker)` triple match would let a prior isolated
child (with its UUID suffix) be incorrectly reused by a later sticky
call.

## Tool contracts

The frontdesk gets two MCP tools at spawn time, on top of the standard
super_agent toolkit.

### `list_agents`

```
list_agents() -> str
```

Returns the same-tenant delegatable-specialist catalog: each active,
non-frontdesk domain agent's name, id (folder slug), and
`routing_description`. The static catalog injected at spawn time may
be stale if specialists have changed since; `list_agents` is the
in-turn refresh path. No permission gate — any super_agent can call it.

### `delegate_to_agent`

```
delegate_to_agent(
  target: str,           # agent id (folder slug)
  prompt: str,           # self-contained, <= 16,000 chars
  context_mode: "isolated" | "sticky" = "isolated",
) -> str  (with optional isError=true)
```

Issues a synchronous NATS request-reply to the orchestrator's
delegation handler, which creates (or finds, for sticky) the child
conversation, enqueues the target's turn, waits up to 300 s for the
target's batch-final event, and returns the synthesized reply.

The frontdesk's LLM is the one that constructs the prompt — RoleMesh
does NOT rewrite, paraphrase, or PII-filter it. The specialist gets it
as a fresh user message in its own conversation.

Permission gate: `coworker.permissions.agent_delegate=True`. Frontdesk
gets it by inheriting `SUPER_AGENT_DEFAULTS`; domain agents default to
False (so a chain delegation `A → B → C` fails at the agent-side gate,
and is independently blocked at the orchestrator-side
`MAX_DELEGATION_DEPTH=1` check).

## Synchronous + parallel semantics

A frontdesk turn may emit multiple `tool_use` blocks in one assistant
message. Each `delegate_to_agent` call uses a distinct
`queue_key = "delegate:{child_conv.id}"`, so the orchestrator runs
them concurrently. The frontdesk LLM blocks on all of them and
synthesizes the final reply.

There's no special-case logic — the per-child queue key falls out of
the per-conversation queue model already used by the existing
scheduler.

## Safety / approval / OIDC

### Safety

Hooks inside the target container apply to delegated calls just like
any other call (input prompt, pre-tool-call, model output stages).
`safety_blocked` flows back through the delegate response with
`isError=true` and the reason in the text body. The frontdesk's system
prompt (FRONTDESK_RULES) contractually requires the reply to quote
both the specialist's name and the literal reason.

### Approval (the v1.2 correction — async + fan-out)

`submit_proposal` from a delegated specialist is **async**: the call
returns immediately and the delegation does NOT wait for human
approval. The 300 s business deadline covers slow LLMs, not approval
queues.

When the approval is later decided (approved+executed, rejected,
skipped, expired, cancelled), the approval engine calls
`channel_sender.send_to_conversation(child_conv.id, message)`. The
child conv lives on a `channel_type='internal'` binding that the
WebUI gateway does NOT subscribe to, so the message would be silently
dropped — the user would never see the approval outcome. This is a
**blocking UX bug**, not a known limitation.

The fix lives in the channel adapter (one site,
`_OrchestratorChannelSender` in `src/rolemesh/main.py`), delegating to
the module-level `send_to_conversation_with_fanout`:

1. Dispatch to the destination conv as before (audit on the child conv
   is intact — admin browsing the child sees the message).
2. If `conv.parent_conversation_id IS NOT NULL`, ALSO dispatch the
   same text to the parent conversation, prefixed
   `[via <target_name>] ` so the user can attribute the message to the
   specialist they don't normally see.

There are 8 distinct `send_to_conversation` call sites under
`approval/` (`executor.py:204`, `executor.py:255`, and 6 inside
`engine.py`). Fixing each by hand would have left at least one path
unfanned. The channel-adapter fix covers all 8 in one place.

We deliberately do NOT widen the `ChannelSender` / `ChannelGateway`
protocols to carry structured `{source, via_target_name}` metadata —
those Protocols are text-only across telegram, slack, and web gateways.
Encoding the via-marker into the text keeps the change scoped:
telegram/slack users see the prefix verbatim (useful annotation), and
the web frontend regex-parses the prefix into a `via X` chip
(`web/src/components/message-item.ts`).

### Approval UI parent-walk

The v1 approvals listing with `?conversation_id=<parent>` (D4 — the old
`/api/admin/approvals` is gone) walks the parent in the shared DB filter
`approval._pending_requests_filter_sql`:

```sql
SELECT * FROM approval_requests
WHERE conversation_id = $1
   OR conversation_id IN (
        SELECT id FROM conversations WHERE parent_conversation_id = $1
      )
ORDER BY created_at DESC
```

Without this, a user viewing the parent conversation would never see
approvals submitted by the specialist while running in the child conv.
The fan-out delivers the *outcome* messages; the parent-walk surfaces
the *list* of pending approvals.

### OIDC pass-through

The user's identity propagates verbatim via the
`X-RoleMesh-User-Id` MCP egress header. The target specialist runs
with the SAME user_id as the parent turn, so per-user MCP scoping
(approval policies, safety rules, OIDC token mirroring) all apply
normally inside the delegated call.

## Routing eval (release-blocking)

The routing-accuracy scorer
(`src/rolemesh/evaluation/scorers/routing_accuracy.py`) reads the
trace and scores per handbook §6 Step 8.3:

- `delegate_to_agent` called with matching `target` → 1.
- `delegate_to_agent` called with wrong `target` → 0.
- No `delegate_to_agent` but `expected_target` set → 0.
- No `delegate_to_agent` and `expected_target=null` → 1.

Plus two multi-delegate behaviours not derivable from the rule
statement:

- A spurious `delegate_to_agent` on an `expected_target=null` sample
  scores 0 (catches the "broadcast greeting to portfolio" failure).
- Multi-delegate with the correct target AND any wrong target scores
  0 (disincentivizes hedge-by-broadcast — even when one of the
  fan-outs is right, the wasted ones are operationally expensive).

### Dataset

`tests/data/routing_dataset.jsonl` — v1.2 launches at **55 cases**
with the composition floor pinned by
`tests/evaluation/test_routing_dataset.py`:

| Slice                       | Floor    | Current |
|-----------------------------|----------|---------|
| Total                       | >= 50    | 55      |
| Adversarial (`metadata.adversarial=True`) | >= 20%   | 22%     |
| Per-target (trading/portfolio/accounting) | >= 5 each | 14 / 14 / 17 |
| No-match (`expected_target=null`) | 5-10     | 8       |
| Failure-passthrough contract | >= 5     | 5       |

### Three-month growth plan

50 cases is the **minimum** a release-blocking gate can run on without
flapping. Within 3 months of shipping v1.2, the dataset MUST grow to:

- **>= 150 total cases.**
- **>= 30 adversarial.**

Sources for growth:

1. Real user prompts that produced routing mistakes in prod — mined
   from the `delegations` audit table by looking for sequences where
   the user's next turn corrected the target.
2. Adversarial templates for each new domain agent added to the
   catalog.
3. Hard no-match cases as new domain capabilities encroach on each
   other's territory.

The dataset size should be tracked on the rolemesh-eval dashboard.
A release-blocking gate that never grows past 50 becomes a rubber
stamp; the growth commitment is what keeps the gate honest.

### How to run the gate

There's no scheduled nightly slot in the project's CI today
(`.github/workflows/ci.yml` runs per-PR). The intended invocation:

```bash
uv run rolemesh-eval run \
  --tenant <uuid> \
  --coworker <frontdesk-id-or-folder> \
  --dataset tests/data/routing_dataset.jsonl \
  --threshold scorers.routing_accuracy_scorer.accuracy>=0.85
```

Exit codes: 0 = pass, 2 = threshold violated, 1 = infrastructure error.
A future scheduled workflow consumes exit-code 2 as the gate signal
on:

- Any change to the frontdesk system prompt or FRONTDESK_RULES.
- Any new domain agent added to the tenant catalog.
- Any `routing_description` edit on an existing specialist.
- Any frontdesk model swap.

## Known v1 trade-offs

These are explicit non-goals, not bugs. Document them so operators
know what to expect from v1.2 and what's on the v1.5 backlog.

1. **Catalog static injection.** New / removed domain agents take
   effect only on next idle restart of the frontdesk containers.
   The `list_agents` tool gives the LLM an in-turn refresh path, but
   the spawn-time catalog is the default. v1.5: catalog hot-reload.

2. **300 s business deadline covers slow LLMs, not approval queues.**
   `submit_proposal` is async; the delegation does not block on human
   approval. If the specialist's turn would exceed 300 s, the
   delegation times out (status='timeout' in the audit) and the
   user gets a "took too long" reply with the specialist name.

3. **Target's internal progress is invisible to the user.**
   `tool_use` / `running` events from the child conv go to the
   internal binding, which the WebUI gateway does not subscribe to.
   The user sees the parent's `delegate_to_agent` chip with a duration
   timer; the specialist's internal tool calls are not surfaced.
   v1.5: child-chip visualization with optional progress streaming.

4. **PII passes through verbatim.** No filtering, no anonymization.
   `prompt_sha256` is recorded for audit dedup only; short prompts
   hash to ~identifiable values. Treat the audit table as
   PII-equivalent.

5. **Frontdesk inherits full super_agent permissions.** The "should
   only route" rule is enforced by FRONTDESK_RULES + the
   release-blocking routing eval, not by stripping permissions. A
   future v1.5 may add a frontdesk-specific permission template.

6. **`CONTAINER_TIMEOUT` defaults to 30 minutes IDLE timer**, not
   wall-clock. A pathological specialist container that stays busy
   is not killed by this timer; the delegation's 300 s business
   deadline catches it from the orchestrator side, but the container
   itself may linger up to the idle timeout before exiting.

7. **Multiple orchestrator replicas not supported.** `_state.coworkers`
   is in-process. A multi-replica deployment would need either sticky
   conversation -> replica routing or in-memory state replication;
   neither is in v1.

8. **Delegation depth strictly 1.** Domain agents default to
   `agent_delegate=False`, and the orchestrator enforces
   `MAX_DELEGATION_DEPTH=1` independently. `A → B → C` chains are
   rejected at both layers; the value `1` is mutation-tested to
   prevent a future "let's allow 2 hops" PR from silently bypassing
   the gate.

9. **During a delegation, the frontdesk is in an active turn.** New
   user messages queue behind the current turn (native SDK behavior).
   Slow paths may delay the next user message by 30-60 s; this is
   visible to the user as the "thinking" indicator.

10. **Sticky session persistence is best-effort.** If `set_session`
    fails (DB blip), the audit still records `success` (the target
    really did succeed) but the next sticky call from this
    parent/target pair starts a fresh session. A log line
    `sticky session_id save failed` flags it for ops.

11. **`prompt_sha256` is audit-dedup only, not a PII shield.** Short
    prompts (e.g. account numbers, dollar amounts) hash to
    identifiable values when an attacker has the search space. The
    column exists to detect double-delivery of the same delegation,
    not to obscure prompt content.

12. **Child conv rows accumulate.** RLS isolates them and the UI hides
    them via `include_children=False`, but storage grows linearly with
    delegation volume. An archival policy is a v1.5 follow-up.

13. **Sticky follow-up turns cold-start the target container.** The
    delegation handler calls `queue.request_shutdown(queue_key)` on
    every terminal event (success, error, safety_blocked, timeout)
    so the target container exits and `executor.execute()` returns.
    The existing `task_scheduler.py:_run_task` uses
    `_TASK_CLOSE_DELAY_S = 10 s` + `notify_idle` to keep containers
    warm across back-to-back tasks on the same `chat_jid`. We
    intentionally do NOT replicate that delay for delegation because
    (a) delegation queue keys are per-child-conv
    (`delegate:{child_id}`) so no other task is waiting to preempt the
    slot, and (b) the simpler shutdown semantics are worth the
    cold-start cost in v1.

    **User-visible consequence**: when a user has a sticky multi-turn
    flow with the same specialist ("then place the order" after "show
    holdings"), the 2nd turn pays a fresh 20-40 s container cold start.
    Functionally correct — we persist `set_session` explicitly and the
    SDK resumes via `resume=session_id` — but the latency is real.
    **v1.5 backlog**: optional warm-keep window for sticky mode that
    mirrors `_TASK_CLOSE_DELAY_S` only when `context_mode='sticky'`.

## Capacity advisory

A frontdesk turn may fan out up to 3 concurrent delegation child
containers in addition to the frontdesk's own container. Required
concurrency budget is therefore:

```
required = peak_concurrent_user_turns * (1 + 3) + 2
```

Where `2` is buffer for cron / scheduled tasks. The webui admin shows
a non-blocking warning when `tenants.max_concurrent_containers` is
below the recommended figure. Advisory only — under-provisioned
tenants still operate correctly, just with queue backpressure instead
of parallelism.

`peak_concurrent_user_turns` is an operator estimate of how many users
will be mid-turn with a frontdesk at peak. It is NOT the count of
frontdesk coworkers in the tenant.

## Out of scope for v1.2

The following are explicitly NOT in v1.2 and live on the v1.5+ backlog:

- Handoff mode (where the user's conversation switches to the
  specialist mid-flow).
- A2A protocol adapter.
- Domain-agent ↔ domain-agent delegation.
- Target's internal progress streamed up to the frontdesk LLM or the
  user UI.
- Frontdesk-specific permission template.
- Admin UI for browsing the `delegations` audit table (v1 uses SQL).
- Multi-orchestrator-replica support.
- Catalog hot-reload.
- Auto-retry on `safety_blocked` with a different target.
- Long-approval blocking / async-notification redesign.
- `docker stop` to forcibly kill a timed-out container.
- A parent-conv state machine (e.g. "switch to trading direct chat").
- Child-chip visualization with progress.
- `wrap_on_output_with_session_save` generalization.
- `role_config` typed accessor / namespacing (kill the
  "dict-for-multiple-purposes" namespace debt).
- Warm-keep window for sticky-mode target containers (see trade-off
  #13).
- HMAC over `prompt_sha256`.
- Adaptive capacity monitoring.

## See also

- `docs/frontdesk-impl/handbook.md` — implementation source of truth,
  including the 9 design steps, the 23-scenario test matrix, the 35
  pitfalls, and the verified-facts list.
- `docs/frontdesk-impl/phase-a-foundation.md` /
  `phase-b-delegation-core.md` / `phase-c-integration.md` — per-session
  scoped work breakdown.
- `src/rolemesh/orchestration/delegation.py` — the delegation
  handler.
- `src/rolemesh/orchestration/catalog.py` —
  `render_agent_catalog` + `FRONTDESK_RULES`.
- `src/rolemesh/db/delegation.py` — DB helpers for the audit table
  and child-conv lifecycle.
