# HITL Approval — S3 handoff notes (orchestrator suspend/resume + sweep + recovery)

Session S3 of `docs/21-hitl-approval-plan.md` (the §8 "hard part"). Built on the
frozen S1 contract and the S2 container hook; no contract drift. Branch:
`feat/hitl-approval-B`.

## What S3 landed

- **`src/rolemesh/container/scheduler.py`** — idle-reaping suspend/resume on
  `GroupQueue`:
  - `_GroupState` gains `idle_handle` (reaping path A, now owned by the queue
    instead of a `main.py` closure so a NATS handler can cancel/re-arm it),
    `awaiting_approval: set[str]` (one entry per concurrently-pending approval —
    a `set`, not a bool, per §6), and `adopted` (restart-rebuilt state).
  - `arm_idle_timer` / `cancel_idle_timer` own the path-A timer.
    `arm_idle_timer` is a **no-op while `awaiting_approval` is non-empty**, which
    closes §8 suspend step 6 ("no path may re-arm idle while suspended") for
    free — a status/tool event mid-approval can't un-suspend.
  - `suspend_for_approval` closes all three paths: cancels the idle timer (A),
    forces `idle_waiting = False` + asserts it (B/C), and adds the request to the
    set. `notify_idle` also early-returns while suspended (defensive path-B
    guard).
  - `resume_from_approval` is idempotent and double-cancel-safe: a `request_id`
    not in the set is a no-op returning `False` (so S2's decision-then-cancel
    can't re-arm twice or mis-clear a sibling). Re-arms a full `IDLE_TIMEOUT`
    **only when the set drains**, and not for scheduled-task containers.
  - `adopt_orphan_container` + `_reap_adopted` for restart recovery: rebuild a
    minimal active `_GroupState` for a container that outlived the orchestrator,
    and tear it down inline when the idle reaper fires (an adopted container has
    no `_run_for_group` `finally`).
  - `GroupQueue(idle_timeout_ms=…)` is now injectable for fast timer tests.
  - `request_shutdown` now captures `job_id` at call time (the adopted reaper
    resets `state.job_id` right after requesting shutdown; the deferred send
    would otherwise target `agent.None.shutdown`).
- **`src/rolemesh/orchestration/approval_coordinator.py`** (new) — the
  orchestrator-side state machine, decoupled from `main.py` globals and from
  NATS/DB for unit-testability:
  - `on_approval_request` → suspend (before the persist await) → persist
    `pending` with `expires_at` → arm an expiry watcher → fail closed on a null
    approver (immediate reject).
  - `on_approval_cancel` → idempotent resume + mark `cancelled` (no decision
    relayed; the container initiated it). Absence in the cache ⇒ clean no-op
    (the decision-then-cancel race).
  - `decide(request_id, …)` (the entry S4's channels call) → first-wins DB
    resolve → **only relays an approve when it won the transition** (never run a
    tool the user didn't authorise for a request that already expired/cancelled)
    → resume.
  - Expiry watcher (`call_later` at `expires_at`) → mark `expired` + resume +
    hard-notify hook. SIGKILLed-container backstop; relays no decision.
  - `recover_pending()` (R2) → scan `list_pending_requests_all_tenants`; not
    expired ⇒ re-adopt + replay suspend + restore decision route (job_id from
    the row) + re-arm expiry; expired ⇒ mark + hard-notify. Idempotent across a
    double run.
  - `request_id == approval_requests.id`: the coordinator persists with the
    container's `request_id` as the row PK so the decision relay (§3.2) routes
    back to the awaiting call.
- **`src/rolemesh/db/approval.py`** — `create_approval_request` gains an optional
  `request_id` to pin the row PK (additive; S1 CRUD callers unaffected).
- **`src/rolemesh/agent/container_executor.py`** — populates
  `AgentInitData.approval_policies` from the tenant's enabled
  `approval_policies` rows (S2 added the field; S3 fills it). `None` when none
  are enabled, so the container keeps the hook off the chain (mirrors
  `safety_rules`).
- **`src/rolemesh/main.py`** — idle timer rewired onto the queue
  (`_reset_idle_timer` → `_queue.arm_idle_timer`; teardown →
  `cancel_idle_timer`). `_start_nats_ipc_subscriptions` creates the
  `ApprovalCoordinator` and subscribes `agent.*.approval_request` /
  `agent.*.approval_cancel` (durable consumers, same pattern as
  `safety_events`); decisions publish on `agent.{job_id}.approval_decision`.
  `recover_pending()` runs at startup after the queue callbacks are wired.

## Tests (all green)

- `tests/container/test_scheduler_approval.py` (9) — timer lifecycle: suspend
  cancels + blocks re-arm; resume re-arms and reaps; suspended container not
  reaped across the wait window; concurrent double approval → only the last
  re-arms; decision-then-cancel → no double re-arm; path-B / path-C suspension;
  adopted container reaped + state cleared.
- `tests/orchestration/test_approval_coordinator.py` (16) — suspend/persist;
  approve/reject relay + resume; null-approver fail-closed; decision race
  (cancel-after-decision idempotent, decide-after-expiry does not relay
  approve); cancel-only path; concurrent independent approvals; expiry; restart
  recovery (re-adopt-not-reaped, expire-past-deadline, idempotent re-run).
- `tests/db/test_approval_crud.py` — added `test_create_request_pins_explicit_id`
  (row PK == container request_id, resolvable by it). Full file: 18 green
  (testcontainers Postgres).
- Adjacent suites unaffected: `test_scheduler.py`, `test_approval_policy.py`,
  `test_approval_hook.py`. `ruff` + `mypy` clean on all changed files.

## Known issue for S4 / ops (cleanup_orphans vs surviving containers)

§8 R2 assumes an approval-held container *survives* an orchestrator restart so
it can be re-adopted. The default Docker runtime, however, force-removes all
`rolemesh-` agent containers via `cleanup_orphans` in
`_ensure_container_system_running` **before** recovery runs, so in that
deployment the re-adopted container is already gone. `recover_pending()` degrades
safely there (the expiry watcher fires, the row is marked `expired`, and
`_reap_adopted` clears the rebuilt state so the conversation is not wedged), and
the re-adoption logic is correct for runtimes/configs where containers do
survive. Genuinely keeping approval-held containers alive across a restart
(excluding them from `cleanup_orphans`) needs a persisted job_id↔container_name
map and is an ops/runtime change — tracked for S4, out of S3 scope.

## For S4

- Decision intake: call `ApprovalCoordinator.decide(request_id, decision=…,
  decided_by=…, note=…)` from the Telegram `CallbackQueryHandler` and the new v1
  WS frame. `decided_by` MUST come from the auth handshake (IDOR guard), never
  the client payload.
- Hard channel: wire `notify_hard(request, outcome)` (reject/expired card) and
  flesh out `notify_status` (the S3 stub emits a web `awaiting_approval` status
  only). Telegram inline ✅/❌ card + `callback_data` `apr:{id}` / `rej:{id}`.
- Target resolution (`conversation_id → channel_bindings → chat_id`, scheduled-
  task fallback) is S4.
