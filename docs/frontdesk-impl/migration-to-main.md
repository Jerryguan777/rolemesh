# Frontdesk — Migration to current `main`

> Status: Plan (execution in progress).
> Source branch: `feat/frontdesk` (16 commits, diverged from `main` at
> `89e0ca7`, 2026-05-09).
> Target: current `main` (+392 commits since divergence; webui-v1, frontend
> api-client, and DB schema were all rewritten in that window).
> Work branch: `feat/frontdesk-v2` (cut fresh from `main`).
> Strategy: **re-port**, not rebase and not merge.

## 1. Why re-port (not rebase / not merge)

A direct integration is not clean. `git merge-tree main feat/frontdesk`
reports **21 conflicting paths**:

* **15 content conflicts** — both sides edited the same regions. The dense
  ones: `db/schema.py` (main rewrote +1019/-340), `rolemesh/main.py`
  (+734/-319), `web/.../chat-panel.ts` (main redesigned it +779/-263).
* **6 modify/delete conflicts** — the branch modified files that `main`
  **deleted and replaced** with a new architecture:

  | Branch modified | Fate on `main` |
  | --- | --- |
  | `src/webui/admin.py` | deleted → `src/webui/v1/` REST package (~30 route modules) + `schemas_v1.py` + `contracts/openapi.yaml` |
  | `src/webui/ws.py` | deleted → `src/webui/v1/ws_stream.py` |
  | `web/src/services/agent-client.ts` | deleted → `web/src/api/client.ts` (REST) + `web/src/ws/v1_client.ts` (stream) |
  | `tests/test_agent_runner/test_approval_handler.py` | deleted on main |
  | `tests/test_agent_runner/test_approval_parity.py` | deleted on main |
  | `tests/test_agent_runner/test_submit_proposal.py` | deleted on main |

The modify/delete set is the decisive factor: the target modules no longer
exist, so the webui admin, WS push, and frontend client work must be
**re-implemented** against the new structure regardless of how we integrate.
A rebase would replay that pain across every one of the 16 commits; a merge
resolves it once but leaves a noisy history and still requires the same
re-implementation. Re-porting onto a fresh branch is the cleanest: copy the
conflict-free new files verbatim, and rewrite only the integration seams.

The good news: the **feature core lives in new files that do not conflict**
(`orchestration/delegation.py`, `catalog.py`, `_chip_throttle.py`,
`db/delegation.py`, the evaluation scorer, `child-agent-chip.ts`,
`tool-name.ts`). The conflicts are all at the integration seams.

## 2. Strategy & conventions

* **One branch** `feat/frontdesk-v2`, cut from `main`.
* **One commit per phase.** Run the phase's verification gate; commit only
  when green. The branch is buildable/testable at every commit so a reviewer
  can read it commit-by-commit.
* **One PR at the end** — opened only after all phases land and the full
  suite + a manual e2e pass green.
* Commits use `git commit -s` (sign-off), **no `Co-Authored-By`**, English
  messages.

### Carry / rewrite / drop

* **Carry verbatim** (new files, no conflict): `orchestration/delegation.py`,
  `orchestration/catalog.py`, `orchestration/_chip_throttle.py`,
  `db/delegation.py`, `evaluation/scorers/routing_accuracy.py`,
  `evaluation/dataset.py`, `tests/data/routing_dataset.jsonl`,
  `agent_runner/tools/context.py`, `web/src/components/child-agent-chip.ts`,
  `web/src/utils/tool-name.ts`, plus the matching test modules.
* **Rewrite** (5 seams): DB schema/modules, `rolemesh/main.py` wiring, the WS
  event contract, the webui v1 coworker fields/approvals, and the frontend
  chat-panel child-chip rendering.
* **Drop**:
  * **Capacity advisory** (`check_frontdesk_capacity`) — unwired dead code
    (only callers are its own 3 unit tests), its `parallel=3` constant maps to
    no enforced cap, and it never blocks anything. The provisioning guidance it
    encoded is folded into the architecture doc instead (Phase 6). Re-adding is
    ~10 lines if ever needed.
  * The 3 `test_agent_runner` modules `main` already deleted — not restored.
  * Stale planning docs (`handbook.md`, `phase-a/b/c.md`) — not re-carried; they
    describe the pre-refactor `admin.py` / `ws.py` structure. Only the
    user-facing architecture doc is ported (updated to v1.5).

### Decisions log

* **D1 — role mapping (agent_role removed on main).** The branch gated
  delegation on `coworkers.agent_role` (`'super_agent'` = frontdesk/router,
  `'agent'` = delegation target). `main`'s Roles & Visibility refactor
  **deleted `agent_role` entirely** (no column, no dataclass field, no
  reader); the role axis is now `AgentPermissions` (`agent_delegate`,
  `task_schedule`, `task_manage_others`) + `visibility`. Chosen mapping
  (user-approved):
  * **frontdesk** = `is_frontdesk=True`; validation requires
    `permissions.agent_delegate=True` (a frontdesk must be able to delegate).
  * **delegation target** = `is_frontdesk=False`.
  * Phase 2: `catalog.py` / `delegation.py` filters `agent_role == "agent"` →
    `not c.is_frontdesk`; target check `agent_role != "agent"` → `c.is_frontdesk`.
  * Phase 4: validation `is_frontdesk=True requires super_agent` →
    `requires permissions.agent_delegate=True`.
  * Tests: every `create_coworker(agent_role="super_agent")` →
    `is_frontdesk=True, permissions=AgentPermissions(agent_delegate=True)`;
    `agent_role="agent"` → `is_frontdesk=False` (default).
* **D2 — `requires_trigger` removed on main.** The branch kept child convs out
  of the message loop via `conversations.requires_trigger=False`; `main` dropped
  that column. The child-exclusion invariant is now carried solely by the
  `parent_conversation_id IS NULL` filter in the conversation list helpers
  (`include_children=False` default). `db.delegation.create_child_conversation`
  was adapted to drop the column; the obsolete
  `test_child_conv_is_created_with_requires_trigger_false` test is dropped
  (the invariant is guarded from the other side by
  `tests/core/test_loader_excludes_children.py`).
* **D3 — `test_coworker_from_state_full_copy.py` moves to Phase 2.** It exercises
  `_coworker_from_state` (a `rolemesh/main.py` function changed in Phase 2), so it
  cannot pass under Phase 1 alone.

## 3. Phases

Each phase = one commit. "Gate" must be green before committing.

### Phase 0 — Branch setup + executor precheck

* **Goal**: branch exists; the one structural prerequisite is confirmed.
* **Tasks**:
  * Cut `feat/frontdesk-v2` from `main`; commit this plan doc.
  * **Precheck (blocking)**: does `ContainerAgentExecutor.__init__` accept a
    `render_catalog=` kwarg on `main`? If not, add it here (Seam 2 depends on it).
  * Confirm whether `Conversation` already carries `requires_trigger` on `main`
    (the branch adds it; avoid a duplicate).
* **Gate**: `import` smoke + existing suite still green.
* **Commit**: `docs(frontdesk): migration-to-main plan` (folds the executor
  precheck if a change was needed).

### Phase 1 — Data layer (Seam 1)

* **Goal**: columns, table, and types exist so every later seam can rely on them.
* **Tasks**:
  * `db/schema.py`: inside `_create_schema()`, add idempotent blocks —
    `conversations.parent_conversation_id` (UUID, FK self, ON DELETE CASCADE) +
    partial index; `coworkers.is_frontdesk` (bool) + `coworkers.routing_description`
    (text); the `delegations` table. Add `await _enable_rls_on(conn, "delegations")`
    in the RLS section. **Note: `main` uses idempotent `CREATE/ALTER ... IF NOT
    EXISTS`, NOT alembic — there is no migrations directory.**
  * `core/types.py`: add `is_frontdesk` + `routing_description` to `Coworker`;
    `parent_conversation_id` to `Conversation`.
  * `db/coworker.py`, `db/chat.py`: thread new fields through `create_*` /
    `update_*` / `_record_to_*`; add `include_children=False` to the conversation
    list helpers.
  * `db/approval.py`: add `conversation_id` parent-walk WHERE clause to
    `list_approval_requests`.
  * Copy `db/delegation.py` verbatim; wire its export into `db/__init__.py`.
* **Tests**: `tests/db/test_delegation.py`, `tests/core/test_coworker_from_state_full_copy.py`,
  `tests/core/test_loader_excludes_children.py`.
* **Gate**: `pytest tests/db tests/core`.
* **Gotcha**: `delegations` MUST have RLS (tenant isolation) wired.
* **Commit**: `feat(frontdesk): schema columns + delegations table + db helpers`

### Phase 2 — Orchestrator + agent-runner (Seam 2)

* **Goal**: delegation works end-to-end, including child-chip publication to NATS.
* **Tasks**:
  * Copy verbatim: `orchestration/delegation.py`, `catalog.py`, `_chip_throttle.py`;
    `agent_runner/tools/rolemesh_tools.py` (`delegate_to_agent` / `list_agents`),
    `agent_runner/tools/context.py`.
  * Rewrite `rolemesh/main.py` (locate each hook against the old branch as a
    reference — `main` moved them in its +734/-319 refactor):
    1. `_coworker_from_state()` → `return cw_state.config`.
    2. add `_emit_child_chip_event_safe()` (the Seam-4 publish source).
    3. `_render_catalog_for_executor()` + pass `render_catalog=` to executors.
    4. startup order: `cleanup_running_delegations()` after `init_database()`,
       before `_load_state()`.
    5. two NATS subscriptions: `agent.*.list_agents.request`,
       `agent.*.delegate.request` (registered after state is loaded).
    6. extract `send_to_conversation_with_fanout()` (~80 lines; the intricate one).
    7. `_convs_for_user_and_cw()` → add tenant_id lookup.
  * Agent-runner adapters (v1.5): `pi_adapter` / `claude_adapter` gain
    `register_delegation` (gated on `agent_delegate`) and
    `register_task_management` (gated on `task_schedule` OR `task_manage_others`);
    backends derive both flags from `init.permissions`.
  * `safety/audit.py`: move `from rolemesh.db import ...` inside
    `DbAuditSink.write()` (lazy) — the agent container image has no `rolemesh.db`,
    so the top-level import crashes any agent that hits the safety hook chain.
* **Tests**: `tests/orchestration/*` (delegate_handler, delegate_unit,
  delegation_chip_events, frontdesk_spawn, list_agents, chip_throttle,
  catalog_no_filesystem_terms), `tests/test_agent_runner/*` (tool_context,
  pi_adapter, send_message_conditional_registration).
* **Gate**: `pytest tests/orchestration tests/test_agent_runner`.
* **Gotcha**: startup ordering (cleanup before load; subscriptions after state).
  Executor `render_catalog` kwarg must already exist (Phase 0).
* **Commit**: `feat(frontdesk): delegation core + orchestrator wiring + agent-runner tools`

### Phase 3 — WS delegation event contract (Seam 4)

* **Goal**: project the child-chip status chunk into typed v1 frames. See
  Appendix A for the full contract design.
* **Tasks**:
  * `contracts/openapi.yaml`: add `WsServerEventDelegation{Started,Progress,ToolUse,Completed}`
    schemas; add the 4 members to the `WsServerEvent` oneOf.
  * `schemas_v1.py`: add the 4 variants to `WsServerEventModel`.
  * `cd web && npm run openapi:gen` → regenerate `web/src/api/generated/types.ts`.
  * `v1/ws_stream.py`: add `_build_child_chip_frame_or_none`; in `_forward_stream`
    intercept `inner.get("kind") == "child_chip"` **before** the progress
    projection branch.
* **Tests**: new ws_stream projection test — 4 phases → 4 frame types, and a
  `phase="status"` chip is NOT mis-projected as `event.run.progress`.
* **Gate**: OpenAPI codegen-freshness test + ws_stream test.
* **Commit**: `feat(frontdesk): v1 WS delegation event contract + ws_stream projection`

### Phase 4 — webui v1 coworker fields + approvals (Seam 3)

* **Goal**: frontdesk fields managed via v1 REST (there is no separate admin view —
  `admin.py` was deleted; the branch only added two fields to coworker CRUD).
* **Tasks**:
  * `schemas_v1.py`: add `is_frontdesk` + `routing_description` (`max_length=500`)
    to the Coworker request/response models.
  * `v1/coworkers.py`: add `_validate_frontdesk_role` (`is_frontdesk=True` requires
    `agent_role='super_agent'`; patch validates the EFFECTIVE post-update values);
    include the fields in the response.
  * `contracts/openapi.yaml`: add the fields to Coworker/CoworkerCreate/CoworkerUpdate;
    regenerate types.
  * `v1/approvals.py`: ensure the `conversation_id` query param flows to the DB
    helper (DB side already done in Phase 1).
* **Tests**: rewrite `tests/webui/test_admin_frontdesk.py` against v1 endpoints
  (role validation, routing_description round-trip, patch-flip, approval parent-walk).
  **Drop `TestCapacityAdvisory`.**
* **Gate**: `pytest tests/webui` + OpenAPI codegen-freshness.
* **Commit**: `feat(frontdesk): v1 coworker frontdesk fields + approval parent-walk`

### Phase 5 — Frontend child-chip UI (Seam 5)

* **Goal**: render delegation child-chips on `main`'s redesigned chat-panel.
* **Tasks**:
  * Copy verbatim: `web/src/components/child-agent-chip.ts`,
    `web/src/utils/tool-name.ts`.
  * `chat-panel.ts`: graft the chip state machine (`activeChildChips` map,
    duration ticker, open/close lifecycle, `[via X]` prefix parsing) onto `main`'s
    `handleV1Event()`; subscribe to the 4 `event.delegation.*` types; switch the
    event shape to the generated snake_case types; clear lingering chips on
    `event.run.completed` (keyed by run_id).
  * `message-item.ts`: render the `via` badge; add `viaTargetName?` to `ChatMessage`;
    add `startedAt` to `AgentStatusState`.
  * `web/src/ws/v1_client.ts`: re-export the 4 new member types.
* **Tests**: frontend component tests (chip render, via badge, delegation event
  dispatch).
* **Gate**: `cd web && npm run test` + `tsc`.
* **Commit**: `feat(frontdesk): child-chip UI on v1 chat-panel + via badge`

### Phase 6 — Eval + docs

* **Goal**: routing-accuracy eval lands; docs reflect v1.5 and the new structure.
* **Tasks**:
  * Copy/adapt `evaluation/scorers/routing_accuracy.py`, `dataset.py`,
    `inspect_glue.py`/`runner.py` changes, `tests/data/routing_dataset.jsonl`,
    and the nightly config.
  * Update the architecture doc to v1.5 (child-chip stream, register-time
    permission gating, the dropped capacity advisory → one-line provisioning note).
  * Optionally extract Appendix A into `docs/frontdesk-impl/seam-4-ws-contract.md`.
* **Tests**: `tests/evaluation/*`.
* **Gate**: `pytest tests/evaluation`.
* **Commit**: `feat(frontdesk): routing-accuracy eval + docs v1.5`

## 4. Commit sequence

| # | Commit message | Gate |
| --- | --- | --- |
| 0 | `docs(frontdesk): migration-to-main plan` (+ executor precheck) | import smoke + suite |
| 1 | `feat(frontdesk): schema columns + delegations table + db helpers` | `pytest tests/db tests/core` |
| 2 | `feat(frontdesk): delegation core + orchestrator wiring + agent-runner tools` | `pytest tests/orchestration tests/test_agent_runner` |
| 3 | `feat(frontdesk): v1 WS delegation event contract + ws_stream projection` | openapi-freshness + ws test |
| 4 | `feat(frontdesk): v1 coworker frontdesk fields + approval parent-walk` | `pytest tests/webui` + openapi-freshness |
| 5 | `feat(frontdesk): child-chip UI on v1 chat-panel + via badge` | `npm run test` + `tsc` |
| 6 | `feat(frontdesk): routing-accuracy eval + docs v1.5` | `pytest tests/evaluation` |

If Phase 0 needs no executor change, fold its doc commit into Phase 1.

## 5. Dependency / critical path

```
P0 ─ P1 ─┬─ P2 ─ P3 ─ P5 ─┐
         └─ P4 ───────────── P7 (final: full suite + e2e + PR)
```

`P3` (WS contract) is the hard prerequisite for `P5` (frontend). `P4` (webui
fields) only depends on `P1` and can proceed alongside `P2`/`P3`.

## 6. Risk register

| Risk | Impact | Mitigation |
| --- | --- | --- |
| `ContainerAgentExecutor` lacks `render_catalog` kwarg | blocks P2 | verify in P0; add the kwarg there |
| `main.py` refactored +734/-319, hook points moved | mis-wired P2 | locate each against the old worktree, not from memory |
| OpenAPI ↔ generated `types.ts` drift | CI freshness red | run `openapi:gen` immediately after every yaml edit |
| chat-panel state model differs from the branch | P5 render bugs | wire one event end-to-end first, then add the other three |
| `delegations` missing RLS | cross-tenant leak | P1 gate includes an RLS test |
| restoring deleted admin/agent_runner tests | dead endpoints reappear | P4 explicitly rewrites; do not restore |

## 7. Code-volume estimate

Engineering estimate (±25%), lean-docs scenario:

| Phase | prod/contract | tests |
| --- | ---: | ---: |
| 1 data layer | ~480 | ~610 |
| 2 orchestrator + agent-runner | ~1,900 | ~3,680 |
| 3 WS contract | ~270 | ~180 |
| 4 webui v1 | ~115 | ~400 |
| 5 frontend | ~365 | ~200 |
| 6 eval | ~280 | ~490 |
| **subtotal** | **~3,410** | **~5,560** |

Plus docs ~1,000 (lean) and ~10 lines of `.gitignore`/`README`. Total landing
**~9,500–10,500 added / ~80 deleted** — same order as the original branch
(+12,014), redistributed: production grows ~+740 (the WS contract and
v1/OpenAPI work barely existed on the old branch), docs shrink (stale planning
docs not re-carried). Tests remain the largest block and the highest
carry-over ratio.

---

## Appendix A — WS delegation event contract (Seam 4)

### A.1 Core decision

Reuse the existing carrier; the typed contract lives only at the v1 boundary.
The branch already publishes child-chip progress via `gw.send_status()`, which
emits a `WebStreamChunk(type="status", content=json(...))` on
`web.stream.{binding}.{chat}` — the exact subject `ws_stream._forward_stream`
already consumes. Therefore: **no new NATS subject**, the orchestrator publish
path is unchanged, and the typed frames are produced by a projection in
`ws_stream.py`, symmetric with `event.run.progress` / `event.approval.*`.

### A.2 Data flow

```
delegation handler (child container output)
   │  ChipThrottleBucket throttles (stays orchestrator-side)
   ▼
_emit_child_chip_event_safe(parent conv's web binding)
   │  gw.send_status(binding, chat, {kind:"child_chip", phase, ...})
   ▼
NATS  web.stream.{parent_binding}.{parent_chat}            (carrier unchanged)
   │     chunk = {type:"status", content: json({kind:"child_chip", ...})}
   ▼
ws_stream._forward_stream → status branch intercepts kind=="child_chip"
   │  _build_child_chip_frame_or_none(active_run_id, inner)   (new, whitelisted)
   ▼
WS frame  event.delegation.{started|progress|tool_use|completed}
   ▼
V1WsClient.onEvent → chat-panel renders <rm-child-agent-chip>
```

### A.3 Carrier payload (orchestrator → NATS, unchanged from branch)

```json
{ "kind": "child_chip", "phase": "open|status|tool_use|close",
  "child_conv_id": "...", "delegation_id": "...",
  "target_folder": "...", "target_name": "...", /* phase-specific */ }
```

### A.4 Server → client frames

Common fields on all four: `type`, `run_id`, `child_conv_id`, `delegation_id`,
`target_folder`, `target_name`. `child_conv_id` is the chip key (concurrent
delegations render as separate chips).

| frame `type` | branch phase | phase-specific fields |
| --- | --- | --- |
| `event.delegation.started` | open | `context_mode?`, `initial_status?` |
| `event.delegation.progress` | status | `status` (open string: running/queued/container_starting) |
| `event.delegation.tool_use` | tool_use | `tool_name` (nullable), `tool_input_preview?` (nullable) |
| `event.delegation.completed` | close | `final_status`, `duration_ms?` |

### A.5 OpenAPI schemas (matches the PR23 WS block style)

```yaml
    WsServerEventDelegationStarted:
      type: object
      required: [type, run_id, child_conv_id, delegation_id, target_folder, target_name]
      properties:
        type: { type: string, enum: [event.delegation.started] }
        run_id: { type: string, format: uuid }
        child_conv_id:
          type: string
          format: uuid
          description: |
            Child (internal) conversation id; the SPA keys each sub-chip
            by this. Mounted on started, unmounted on completed.
        delegation_id: { type: string, format: uuid }
        target_folder: { type: string }
        target_name: { type: string }
        context_mode:
          type: string
          nullable: true
          description: "'isolated' | 'sticky' — delegation context carry-over."
        initial_status: { type: string, nullable: true }

    WsServerEventDelegationProgress:
      type: object
      required: [type, run_id, child_conv_id, delegation_id, target_folder, target_name, status]
      properties:
        type: { type: string, enum: [event.delegation.progress] }
        run_id: { type: string, format: uuid }
        child_conv_id: { type: string, format: uuid }
        delegation_id: { type: string, format: uuid }
        target_folder: { type: string }
        target_name: { type: string }
        status:
          type: string
          description: |
            Specialist container phase: running / queued / container_starting.
            Open string (mirrors event.run.progress.status) so a new
            orchestrator kind renders via fallback rather than failing validation.

    WsServerEventDelegationToolUse:
      type: object
      required: [type, run_id, child_conv_id, delegation_id, target_folder, target_name, tool_name]
      properties:
        type: { type: string, enum: [event.delegation.tool_use] }
        run_id: { type: string, format: uuid }
        child_conv_id: { type: string, format: uuid }
        delegation_id: { type: string, format: uuid }
        target_folder: { type: string }
        target_name: { type: string }
        tool_name:
          type: string
          nullable: true
          description: Tool the specialist is invoking; null if unknown.
        tool_input_preview:
          type: string
          nullable: true
          description: |
            Truncated preview of the specialist's tool input (orchestrator
            truncates server-side; the name carries the semantic, same as
            event.run.progress.input_preview).

    WsServerEventDelegationCompleted:
      type: object
      required: [type, run_id, child_conv_id, delegation_id, target_folder, target_name, final_status]
      properties:
        type: { type: string, enum: [event.delegation.completed] }
        run_id: { type: string, format: uuid }
        child_conv_id: { type: string, format: uuid }
        delegation_id: { type: string, format: uuid }
        target_folder: { type: string }
        target_name: { type: string }
        final_status:
          type: string
          description: "success | error | cancelled | timeout — delegation terminal state."
        duration_ms: { type: integer, nullable: true }
```

Add all four to the `WsServerEvent` oneOf union, and add matching variants to
the Pydantic `WsServerEventModel` in `schemas_v1.py`.

### A.6 Server-side projection (`v1/ws_stream.py`)

```python
def _build_child_chip_frame_or_none(
    run_id: str, inner: dict[str, Any]
) -> dict[str, Any] | None:
    """Project an orchestrator child-chip status payload (frontdesk v1.5)
    to an ``event.delegation.*`` frame.

    Delegation child-progress rides the PARENT conversation's
    ``web.stream.*`` carrier as a ``kind="status"`` chunk tagged
    ``kind="child_chip"`` (see rolemesh.main._emit_child_chip_event_safe).
    We split it off the per-turn progress projection and map the four
    lifecycle phases to distinct typed frames, mirroring event.run.*.
    Field whitelisting matches the progress/approval posture so a future
    orchestrator-side key can't leak to the browser. Unknown phases drop.
    """
    common: dict[str, Any] = {"run_id": run_id}
    for key in ("child_conv_id", "delegation_id", "target_folder", "target_name"):
        v = inner.get(key)
        if not isinstance(v, str) or not v:
            return None  # all four identity fields are required
        common[key] = v

    phase = inner.get("phase")
    if phase == "open":
        frame = {"type": "event.delegation.started", **common}
        for key in ("context_mode", "initial_status"):
            v = inner.get(key)
            if isinstance(v, str):
                frame[key] = v
        return frame
    if phase == "status":
        status = inner.get("status")
        if not isinstance(status, str) or not status:
            return None
        return {"type": "event.delegation.progress", **common, "status": status}
    if phase == "tool_use":
        tn = inner.get("tool_name")
        frame = {
            "type": "event.delegation.tool_use",
            **common,
            "tool_name": tn if isinstance(tn, str) else None,
        }
        ti = inner.get("tool_input")          # renamed at the boundary
        if isinstance(ti, str) and ti:
            frame["tool_input_preview"] = ti
        return frame
    if phase == "close":
        fs = inner.get("final_status")
        if not isinstance(fs, str) or not fs:
            return None
        frame = {"type": "event.delegation.completed", **common, "final_status": fs}
        dms = inner.get("duration_ms")
        if isinstance(dms, int):
            frame["duration_ms"] = dms
        return frame
    return None  # unknown phase degrades gracefully
```

Interception in `_forward_stream`'s `status` branch (must run before the
progress projection, else a `phase="status"` chip is mis-projected as the
parent's `event.run.progress`):

```python
elif kind == "status":
    inner = json.loads(data.get("content", "{}"))
    if inner.get("kind") == "child_chip":
        chip = _build_child_chip_frame_or_none(run_id, inner)
        if chip is not None:
            await _send_event(ws, chip)
    else:
        progress = _build_progress_frame_or_none(run_id, inner)
        if progress is not None:
            await _send_event(ws, progress)
```

### A.7 Decisions

* **Four distinct types, not one `phase`-tagged type.** Matches `main`'s
  convention (`event.run.started/progress/completed`, `event.approval.requested/
  resolved`): OpenAPI `oneOf` + `type` discriminator codegens a clean tagged
  union, and per-type `required` sets express each phase's mandatory fields
  (progress needs `status`, completed needs `final_status`) — a single type
  could only make them all optional.
* **`run_id` is required.** Delegation only happens mid-parent-turn, so the
  parent WS connection's `active_run_id` is always set; `_forward_stream`'s
  existing `if run_id is None: continue` guard naturally anchors chips to the
  parent run. Bonus: the SPA can drop lingering chips on `event.run.completed`
  by `run_id`.
* **`tool_input` → `tool_input_preview`.** Mirrors `event.run.progress.input_preview`
  and its truncation semantics. Truncation happens orchestrator-side; the
  projection only renames.
* **Throttle stays orchestrator-side.** `ChipThrottleBucket` (500ms
  last-write-wins, `flush_all()` on close) runs in the delegation handler; the
  projection is stateless.
* **Whitelist posture.** Each frame carries only explicitly-listed fields, so a
  future orchestrator-side key (e.g. `prompt_sha256`) can never reach the browser.
