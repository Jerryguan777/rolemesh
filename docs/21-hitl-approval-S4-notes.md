# HITL Approval — S4 handoff notes (delivery + dual-channel notify + E2E)

Session S4 of `docs/21-hitl-approval-plan.md` (§10 S4 + §9 R4). Built on the
frozen S1 contract, the S2 container hook, and the S3 orchestrator coordinator;
no contract drift. Branch: `feat/hitl-approval-B`.

S4 is the human I/O layer only: it connects the S3 coordinator's two delivery
hooks (`notify_status` / `notify_hard`) to real Telegram / web output, adds the
two decision intakes (Telegram callback, web WS frame), and proves the whole
chain with automated tests. It does **not** touch the suspend/resume state
machine, the policy snapshot, or container lifecycle.

## What S4 landed

- **`src/rolemesh/orchestration/approval_notify.py`** (new) — `ApprovalNotifier`,
  the card-lifecycle layer. Decoupled from DB + channels by injected callables
  (so it is unit-tested against fakes, no broker/Postgres/Telegram):
  - `notify_status(req)` (the coordinator's soft hook) → resolves the request's
    conversation → binding → chat, caches a `_CardRef`, and delivers the
    ✅/❌ card (Telegram inline keyboard / a web `approval.requested` event).
  - `notify_hard(req, kind)` (the coordinator's hard hook) and `mark_outcome`
    → deterministically edit that same card to `✅ Approved` / `❌ Rejected` /
    `⏰ Expired` (English per repo convention). Terminal, so the cache entry is
    popped; a racing second resolve no-ops.
  - **Target resolution** (§10 S4): `conversation_id → channel_bindings →
    channel_chat_id`; a scheduled-task request (`conversation_id is None`) falls
    back to the coworker's **most recently active** conversation
    (`last_agent_invocation`, then `created_at`).
  - `card_ref(request_id)` exposes the authoritative `(tenant, conversation,
    chat)` the Telegram decision funnel authorises a tap against.
- **`src/rolemesh/channels/telegram_gateway.py`** — fresh inline-keyboard card
  (no inline kb existed on main). `send_approval_card` returns the `message_id`
  for a later edit; `edit_approval_card` drops the buttons on resolve. A
  `CallbackQueryHandler` (pattern-restricted to `apr:`/`rej:`) routes taps
  through the module-level `parse_approval_callback` + `_handle_approval_callback`
  helpers (testable with a stub `Update`) to a `set_on_approval_decision`
  callback. `callback_data` is `apr:{request_id}` / `rej:{request_id}` (~40 B,
  within the 64 B limit) and carries **no** approver identity.
- **`src/webui/schemas_v1.py` + `src/webui/v1/ws_stream.py`** — new v1 WS frames:
  - server → browser: `event.approval.requested` / `event.approval.resolved`
    (out-of-band, like `event.message.appended`; forwarded by a third
    `_forward_approval` task off the new `web.approval.{binding}.{chat}` subject
    with explicit field whitelisting).
  - browser → server: `request.approval_decision` (`request_id` + verb + note).
    The handler stamps `decided_by`/`tenant_id`/`conversation_id` from the
    **verified ticket**, never the frame, then publishes
    `web.approval_decision.{binding}.{chat}`.
- **`src/rolemesh/channels/web_nats_gateway.py`** — `send_approval_event`
  publishes the card events; a new `web.approval_decision.*.*` subscription
  (`set_on_approval_decision`) hands decisions to the orchestrator funnel.
- **`src/rolemesh/main.py`** — wires the notifier into the coordinator
  (`notify_status`/`notify_hard`), registers both gateway decision callbacks
  **before** bindings are added (the bot instances capture the callback at
  construction; the funnels late-bind the coordinator/notifier globals, which
  exist by the time any card can be tapped), and hosts the two decision funnels:
  - `_telegram_approval_decision` — resolves `(tenant, conversation, chat)` from
    the notifier's card cache, requires the tapping Telegram account to resolve
    via `user_channel_identities` to a RoleMesh user **in that tenant**, refuses
    a tap from a foreign chat, then calls `coordinator.decide(...)`.
  - `_web_approval_decision` — re-derives tenant from the binding row and the
    conversation from `(binding, chat)`, then `coordinator.decide(...)`.
  - On a winning **approve**, the funnel calls `notifier.mark_outcome(id,
    "approved")` (reject/expire edit the card via the coordinator's
    `notify_hard`).
- **`coordinator.decide`** gained additive IDOR guards
  `expected_tenant_id` / `expected_conversation_id`: a request whose pending row
  doesn't match is refused **before** any DB write or relay. Internal callers
  (fail-closed reject, expiry) omit them and keep the legacy behaviour.
- **OpenAPI + TS client**: added the three frames to `contracts/openapi.yaml`
  (+ discriminator mappings), regenerated `web/src/api/generated/types.ts`, and
  added `V1WsClient.sendApprovalDecision` + the two event type re-exports.
  Fixed the pre-existing stale `_EXPECTED_*` discriminator sets in
  `tests/test_openapi_contract.py` (they were already red — missing
  `request.stop` / `event.run.progress` / `event.message.appended`).

## R4 (resolved + documented)

Safety `require_approval` **stays a hard block and does NOT enter HITL**. HITL
gates only tenant MCP-tool *policy* matches (the block-and-await hook in
`agent_runner`). Pinned in code at the orchestrator MODEL_OUTPUT verdict branch
(`src/rolemesh/main.py`, `verdict.action in ("block", "require_approval")`) and
here. There are two distinct gate types by design; do not merge them.

## IDOR posture (the headline correctness property)

The decision wire carries only `request_id` + verb on both channels. The
approver identity is always server-resolved:

- **Telegram**: from the Telegram-authenticated `from_user.id` via
  `user_channel_identities`, scoped to the card's tenant; a tap from a foreign
  chat or an unlinked sender is refused before `decide`.
- **Web**: from the verified WS ticket (`user_id`/`tenant_id`), with the tenant
  re-derived from the authenticated subject binding.
- **Coordinator**: `decide`'s `expected_tenant_id`/`expected_conversation_id`
  guard is the final chokepoint — a guessed UUID can't cross a tenant or
  conversation boundary even if a channel funnel regressed.

## Tests (all green, no broker / Postgres / live channels)

- `tests/orchestration/test_approval_notify.py` (10) — target resolution
  (conversation + scheduled fallback-to-most-recent + missing target/binding),
  Telegram deliver→edit, web requested→resolved, approve/reject/expire card
  text, failed-send skips edit, restart (no cached card → no-op).
- `tests/orchestration/test_approval_decision_funnel.py` (8) — the full chain
  end to end through the **real** coordinator + notifier and the main.py
  funnels: card delivery → tap/frame → `decide` → relay to the container
  (`publish_decision`) → hard card edit. Includes the IDOR refusals (unlinked
  sender, foreign chat, cross-tenant web frame).
- `tests/channels/test_telegram_approval.py` (8) — callback parse edge cases,
  64 B limit, keyboard `callback_data`, dispatch carries authenticated identity,
  malformed/erroring taps still answer.
- `tests/webui/test_ws_approval.py` (11) — frame schema validation (incl.
  "frame can't smuggle `decided_by`"), carrier→browser field whitelisting,
  decision relay stamps ticket identity over a hostile payload field.
- `tests/webui/test_v1_ws_approval_frame.py` (2) — real-transport `TestClient`
  WS: decision frame → NATS publish with ticket identity; pushed approval event
  → forwarded browser frame (internal keys stripped).
- `tests/orchestration/test_approval_coordinator.py` (+6) — IDOR guard
  refuse/allow + `notify_hard` fires on reject/expiry, never on approve.
- OpenAPI freshness + contract suites green (36).
- The container-side unblock that consumes `agent.{job_id}.approval_decision`
  is the S2 hook's responsibility and is covered by the S2 tests; here the
  relay payload on that subject is the asserted seam.

## What needs human/manual review (not automatable unattended)

- A real Telegram round-trip (tap a live ✅/❌ button) and a real browser SPA
  card render + click. The automated suite mocks the outermost Telegram/WS
  boundaries; the wire contracts (callback_data, NATS subjects, frame shapes)
  are pinned, but a live click-through is worth one manual pass.
- The SPA **card UI** itself (rendering `event.approval.requested`, wiring a
  button to `sendApprovalDecision`, updating on `event.approval.resolved`) is
  frontend work beyond the v1 client method added here.

## Known limitations / follow-ups

- **Card-location cache is in-memory.** After an orchestrator restart the
  Telegram `message_id` is lost, so a hard edit (reject/expire) is best-effort —
  the `approval_requests` row stays authoritative. Same restart degradation the
  S3 coordinator documents.
- **Web "survive disconnect"** is delivered as live push only. A reconnecting
  browser re-rendering an in-flight card from the DB needs a small REST read
  (`list_pending_requests_for_tenant` filtered by conversation) — deferred to
  S5's policy-CRUD/UI session; the row is already the source of truth.
- **`cleanup_orphans` vs surviving containers** remains the S3-documented ops
  item; out of S4 scope.
