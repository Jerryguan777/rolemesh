# A1 — Clean Self-Approval HITL (approve → wake-up → resume → one-shot re-execute)

Status: PLAN (awaiting review before coding)
Baseline branch: `main`
Work branch: `claude/hitl-approval-agent-sdk-9Q6Ff`
Rollout: two-stage — **Stage 1: pure deletion** (worker + SoD), **Stage 2: build A1**.

---

## 0. Why / what changes

Today (scheme **A**): a `PreToolUse` hook blocks the matched tool call, an
approval request is persisted, and after approval a **separate
`ApprovalWorker`** replays the actions out-of-band through the credential
proxy. The agent's ReAct loop never sees the real result.

Target (scheme **A1**): after approval, the orchestrator **wakes the
conversation** — spawns a fresh container that **resumes the prior session**,
tells the agent "your proposed action was approved, continue", and carries a
**one-shot allowlist** so the agent re-issues *exactly that* tool call and the
gate lets it through. The tool then executes **inside the agent's own loop**
(still via the credential proxy), the real result flows back to the LLM, and
the agent finishes the task and reports to the user in its own words.

No ApprovalWorker. No out-of-band execution. Self-approval only (requester is
the sole approver) — all separation-of-duties (SoD) residue removed.

Design decisions locked for A1:
- **Self-approval only.** Requester == approver, always.
- **Wake-up = a normal scheduler run**, triggered internally instead of by an
  inbound channel message. Reuses per-conversation serialization.
- **Resume = existing `session_id` turn-resume.** No mid-tool-call checkpoint
  (that was the Pi-only B1 variant; explicitly out of scope).
- **One-shot allowlist keyed by `action_hash`.** Exact-match only; any drift is
  re-gated (safe by construction). This is the accepted drift trade-off.
- **Decision-side expiry kept; execution-side reconcile deleted.**

---

## STAGE 1 — Deletion plan (worker + SoD)

Goal: land a single reviewable commit that removes the ApprovalWorker /
out-of-band execution machinery and all SoD residue, leaving a **compiling,
coherent, self-approval-only** approval module that still does:
match → persist pending → notify requester → decide (approve/reject) →
decision-side expiry. (After Stage 1, approval simply has *no executor*; the
"what happens on approve" seam is where Stage 2 plugs in.)

### 1A. Files deleted outright
- `src/rolemesh/approval/executor.py` (entire ApprovalWorker, self-contained)
- `tests/approval/test_executor.py`
- `tests/approval/e2e/test_e2e_reconcile.py`
- `tests/approval/e2e/test_e2e_idempotency_isolation.py` (idempotency-key contract is worker-only)
- `tests/approval/e2e/test_e2e_long_batch.py` (worker batch execution)
- `tests/approval/e2e/test_e2e_mcp_application_error.py` (worker MCP exec error mapping)
- `tests/approval/e2e/test_e2e_mixed_batch_report.py` (worker execution report)

### 1B. Worker wiring removed
- `src/rolemesh/main.py:1831-1838` — delete `from ...executor import ApprovalWorker`,
  the `ApprovalWorker(...)` instantiation and `await approval_worker.start()`.
  Also `await approval_worker.stop()` at ~2067.
- `src/rolemesh/main.py:1617` — update the "still bound for the ApprovalWorker"
  comment on the credential-proxy startup. **Keep the proxy itself** (agents and
  Stage-2 in-loop execution still use it).

### 1C. `approval.decided.*` removal
- `src/rolemesh/approval/engine.py`:
  - Delete `_publish_decided()` (807-822).
  - Delete its call sites: 444 (`handle_proposal` post-approve auto-execute),
    792 (`handle_decision`), 976 (reconcile republish).
- `src/rolemesh/ipc/nats_transport.py:116-124` — drop `"approval.decided.*"`
  from the `approval-ipc` stream subjects. **Keep `"approval.cancel_for_job.*"`**
  (Stop-cascade from containers). Update comment at :111.

> Note: Stage 2 replaces the deleted `_publish_decided` call in
> `handle_decision` with the wake-up trigger (§2E). In Stage 1 the approve path
> simply marks the row approved + publishes the web-resolved event and stops —
> nothing executes yet. That is the intended clean seam.

### 1D. Execution-side DB layer (`src/rolemesh/db/approval.py`)
- Delete `claim_approval_for_execution()` (648-681).
- Delete `list_stuck_executing_approvals()` (785-808).
- Delete `list_stuck_approved_approvals()` (763-782).
- Remove all three from `__all__`.
- **Keep** `set_approval_status()` (used for pending→approved/rejected too),
  `list_expired_pending_approvals()` (decision expiry), and
  `decide_approval_request_full()` (the atomic decide CTE).

### 1E. Maintenance loop (`src/rolemesh/approval/expiry.py`)
- Keep `run_approval_maintenance_loop()` and the `expire_stale_requests()` call (:44).
- Delete the `reconcile_stuck_requests()` call (:48).
- `src/rolemesh/approval/engine.py`:
  - Delete `reconcile_stuck_requests()` (967-995).
  - Remove imports `list_stuck_approved_approvals`, `list_stuck_executing_approvals` (54-55).
  - Keep `expire_stale_requests()` (948-965).

### 1F. Execution notifications (`src/rolemesh/approval/notification.py`)
- Delete `format_execution_started()` and `format_execution_report()`.
- Delete `format_execution_stale_message()` (reconcile-only).
- Keep `format_decision_message`, `format_expired_message`,
  `format_cancelled_message`, `format_skipped_message`.

### 1G. Status enum trim (`src/rolemesh/db/schema.py`)
Decision: **clean break** (greenfield DB; no prod rows to preserve — confirm).
- `approval_requests` CHECK (998-1003): keep
  `pending, approved, rejected, expired, cancelled, skipped`; drop
  `executing, executed, execution_failed, execution_stale`.
- `approval_audit_log` action CHECK (1071-1075): same trim.
- Delete index `idx_approval_requests_executing` (1047-1048).
- Mirror the enum trim in `src/rolemesh/approval/types.py` docstrings/validators.

### 1H. SoD removal — `approver_user_ids` (truly unused, full delete)
- `src/rolemesh/db/schema.py:946` — drop column.
- `src/rolemesh/approval/types.py` — drop field from `ApprovalPolicy` (31) + `to_dict` (49).
- `src/rolemesh/db/approval.py` — remove from create/update/read (59,67,87,95,101-102,116,207,236-237).
- `src/webui/v1/approval_policies.py:42-44` — remove the "intentionally not projected" comment (moot once gone).
- `tests/approval/test_engine.py` (~145-195) — delete the "SoD seam is ignored" test.

### 1I. SoD removal — `resolved_approvers` (DECIDED: FULL DELETE)
Self-approval only; no SoD seam retained. The approver is *always* the
requester (`user_id`), so the column carries zero information and is dropped.
- `src/rolemesh/db/schema.py:1004` — drop the `resolved_approvers` column.
- `src/rolemesh/approval/types.py:94` — drop the field from `ApprovalRequest`.
- `src/rolemesh/db/approval.py`:
  - Drop `resolved_approvers` from read/build/INSERT (298, 314, 406).
  - Rewrite the decide CTE authorization (`616`) from
    `$3::uuid = ANY(b.resolved_approvers)` to `$3::uuid = b.user_id`
    (caller must be the requester). Keep the 403/409/200 disambiguation.
- `src/rolemesh/approval/engine.py` — delete `_resolve_approvers()` (1000-1024)
  entirely and its two call sites (452-454 in `handle_proposal`, 556-558 in
  `handle_auto_intercept`); the create path no longer passes an approvers list.
  The empty-requester edge (bot-chained / system turn) routes to the existing
  owner-FYI path in `handle_auto_intercept` — preserve that branch but key it
  off `not user_id` directly instead of `_resolve_approvers() == []`.
- `src/rolemesh/approval/notification.py:124-130` — replace the
  `for approver_id in request.resolved_approvers` fan-out with a single lookup
  on `request.user_id`.
- `src/webui/v1/approvals.py:156-167` — `scope=mine` filter becomes
  `r.user_id == user.user_id` (drop the `resolved_approvers` membership test).
  `scope=all` (admin) unchanged.

### 1J. Stage-1 exit criteria
- `ruff` + `mypy` clean; full non-deleted approval test suite green.
- Manual trace: propose → match → pending → notify requester → approve →
  row=`approved` + web-resolved event fired → **nothing executes** (expected).
- `grep -rn "ApprovalWorker\|approval.decided\|claim_approval_for_execution\|approver_user_ids\|resolved_approvers\|_resolve_approvers\|execution_failed" src/` returns nothing in live paths.

---

## STAGE 2 — Build A1 (approve → wake-up → resume → one-shot re-execute)

### 2A. Extend `AgentInitData` (`src/rolemesh/ipc/protocol.py:36-105`)
Add two optional fields (keep `frozen=True`, default-safe for
`from_dict_filter_unknown`):
```python
resume_reason: str | None = None              # e.g. "approved:<request_id>"
pre_approved_action_hashes: list[str] = field(default_factory=list)
```
Serialize/deserialize already generic (`asdict`/filtered) — no extra work.

### 2B. Populate on spawn (`src/rolemesh/agent/container_executor.py:405-426`)
Thread the two new fields from `AgentInput` into the constructed
`AgentInitData`. Add matching fields to `AgentInput` (the orchestrator-side
input dataclass) so `_run_agent` can pass them.

### 2C. Container injects resume context (`src/agent_runner/main.py:396-406`)
After base prompt assembly, if `init.resume_reason`:
```python
prompt = (
  "[APPROVAL GRANTED] Your previously proposed action was approved by the user. "
  "Re-issue that exact tool call to carry it out, then continue the task.\n\n"
) + prompt
```
(Final wording in code review; must not claim the action already happened.)

### 2D. One-shot gate allowlist (`src/agent_runner/hooks/handlers/approval.py:66-119`)
- Carry `pre_approved_action_hashes` into the handler via `ToolContext`
  (`src/agent_runner/tools/context.py` + construction in `agent_runner/main.py`).
- In `on_pre_tool_use`, after `action_hash = compute_action_hash(...)` (:87) and
  **before** the block: if `action_hash` in the allowlist → **consume it**
  (remove, so it's strictly one-shot) and `return None` (allow). Otherwise the
  existing match→block path runs unchanged (so any drifted/extra action is
  re-gated → new approval request).
- `compute_action_hash` (`agent_runner/approval/policy.py:255-285`) is the same
  deterministic hash already stored in `approval_requests.action_hashes`, so the
  orchestrator can hand the container the exact value(s) to pre-approve.

### 2E. Wake-up trigger (`src/rolemesh/approval/engine.py` `handle_decision` 733-805)
Replace the deleted `_publish_decided` (approve branch) with a call into a new
orchestrator seam, e.g. `on_approved(request)`, injected into the engine at
construction (engine stays infra-agnostic; main.py wires the concrete impl).
The impl:
1. Looks up conversation/coworker/tenant from the `ApprovalRequest`
   (`conversation_id`, `coworker_id`, `user_id` — all present; session via the
   `sessions` table by `conversation_id`).
2. Stashes `resume_reason="approved:<id>"` + `pre_approved_action_hashes =
   request.action_hashes` for the next run of that conversation.
3. Calls the scheduler entry point `enqueue_message_check(conversation_id, ...)`
   (`src/rolemesh/container/scheduler.py:139-170`) — the **same** path inbound
   messages use, so per-conversation serialization (`_GroupState.active`,
   `pending_messages`, `_drain_group`) prevents collisions with an in-flight turn.
Reject branch: just the existing web-resolved event + a decision message to the
requester's conversation (no wake-up). Expiry: same as reject (agent isn't
woken; user is told it expired on their next turn, or via a decision message).

### 2F. Carrying the per-conversation resume payload
`enqueue_message_check` doesn't take a prompt today; the run prompt is built in
`_run_agent` from missed messages. Add an optional per-conversation
"pending resume" slot on `ConversationState` (orchestrator state) that
`_run_agent` consumes when building the next `AgentInput`:
- if a resume payload is queued → set `AgentInput.resume_reason` +
  `pre_approved_action_hashes`, and seed the prompt with the approval-granted
  preface (or merge with any genuinely-new user messages).
- clear it after one consumption (one-shot at this layer too).

### 2G. Drift & staleness handling (explicit, accepted)
- **Drift** (agent re-issues a different call): the changed `action_hash` misses
  the allowlist → normal gate → new approval request. Safe; may cause a second
  approval round. Acceptable per locked decisions.
- **No re-issue** (agent thinks it's done): mitigated by the 2C preface +
  system-prompt guidance. Not a safety issue (nothing wrong executes); a UX one.
- **New user messages during the wait**: merged in 2F; serialization in 2E means
  the wake-up either runs after the in-flight turn or is itself the turn that
  also picks up the new messages.
- **Multi-action proposals**: `pre_approved_action_hashes` is a list; all
  approved hashes are pre-approved for the resumed turn. Each is one-shot.

### 2H. Stage-2 exit criteria
- E2E: propose action under a matching policy → blocked, pending persisted,
  requester notified → approve via REST → conversation wakes → agent re-issues
  the call → gate allows (one-shot) → tool executes in-loop via proxy → real
  result in transcript → agent reports completion. Assert the tool actually ran
  **once** and the allowlist entry was consumed.
- E2E: approve then agent drifts → second approval request created (no rogue
  execution).
- E2E: reject → decision message, no wake-up, no execution.
- E2E: expiry → pending→expired, no wake-up.
- Both backends (Claude SDK + Pi) pass — A1 uses only the shared
  turn-resume + shared hook gate, so it is backend-agnostic.

---

## Decisions (locked)
1. **DB strategy** — **DECIDED: clean break (§1G).** Drop the execution statuses
   from the CHECK constraints outright; no legacy-status retention.
2. **Resume prompt wording** (2C) and **system-prompt** additions for
   "report truthfully, re-issue the approved call, don't claim premature
   success" — to be finalized in Stage-2 code review.
3. **Expiry UX** — **DECIDED: no proactive notification.** Expiry just marks the
   row `expired` (existing `expire_stale_requests`); the user learns of it on
   their next turn. No decision message is pushed on expiry. (Reject still sends
   its decision message; expiry does not.)
4. **`resolved_approvers` column** — **DECIDED: FULL DELETE (§1I).** No SoD seam
   retained anywhere. Approver is always the requester (`user_id`); the column,
   the dataclass field, `_resolve_approvers()`, and the array-membership auth
   check are all removed and replaced with direct `user_id` equality. Clean
   self-approval only.
