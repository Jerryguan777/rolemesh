# Phase C — Integration (Step 7, 8, 9)

> Branch: `feat/frontdesk` · Estimated session length: ~1,390 LOC
> (540 prod + 350 tests + 500 docs).
> Output: 3 commits on `feat/frontdesk`. Not pushed.
> Depends on: Phase A and Phase B complete on `feat/frontdesk`.

---

## Session Prompt

> Paste the block below as the first message in a fresh Claude Code
> session. It is self-contained.

```
You are implementing Phase C (integration + docs) of Frontdesk Agent
v1.2 in the RoleMesh codebase. Working directory:
/home/jerry/ai/rolemesh-3. Branch: feat/frontdesk. Phase A and B are
already complete (6 commits on this branch). Verify with
`git log --oneline -10`.

REQUIRED READING ORDER:
1. docs/frontdesk-impl/handbook.md  — source of truth. Especially §6
   Step 7-9 and §4 decisions #10, #11 (approval parent-walk and
   fan-out).
2. docs/frontdesk-impl/phase-c-integration.md  — this phase doc.

Phase C covers Steps 7, 8, 9:
  Step 7: WebUI admin (is_frontdesk toggle, routing_description
          textarea, capacity advisory), conversation list filter,
          approval list parent-walk, and APPROVAL FAN-OUT at the
          channel adapter layer.
  Step 8: Routing-accuracy eval scorer + dataset + nightly wiring.
  Step 9: Update docs/frontdesk-architecture.md from v1 to v1.2 + a
          one-line README mention.

THE FAN-OUT IS THE CENTERPIECE OF PHASE C. Read handbook §6 Step 7.6
carefully. Key points:

  - 8 distinct call sites under approval/ go through
    _channel.send_to_conversation: executor.py:204, executor.py:255,
    and 6 inside engine.py. Wrapping each is brittle.
  - Fix is at the CHANNEL ADAPTER layer
    (MessageChannel.send_to_conversation): after dispatching to the
    target conversation_id, check if it has a parent_conversation_id,
    and if yes, also dispatch to parent with metadata
    {source: "delegation_fanout", via_target_name: ...}.
  - Non-regression: regular conversations have
    parent_conversation_id IS NULL → fan-out branch skipped.
  - Frontend message-item renders the via_target_name as a chip.

CONFIRM BEFORE CODING:
  - Locate the actual `send_to_conversation` def the orchestrator
    uses (grep — likely in main.py around the _channel builder, or
    in a channel module). That is the function to modify. Do NOT
    duplicate the logic across multiple channel implementations
    without confirming both are used.
  - Locate the existing eval scorer pattern (Step 8.1 pre-check
    grep). Reuse existing infrastructure if it exists.

Work commit-by-commit:
  Commit 7 — WebUI admin + parent-walk + channel-level fan-out
  Commit 8 — routing-accuracy eval scorer + dataset + nightly
  Commit 9 — docs/frontdesk-architecture.md v1.2 + README mention

After each commit:
  uv run pytest && uv run mypy src && uv run ruff check src tests
must pass. Also: when frontend files change, run any project frontend
checks if present. Use `git commit -s`. Prefix `feat(frontdesk):`.
Do not push.

Stop and ask when:
  - The send_to_conversation function is in an unexpected location
    or there are multiple implementations with no clear "primary".
  - The eval scorer infrastructure doesn't exist and you need to
    decide whether to build it from scratch or scope-cut to a
    minimal stub.
  - The approval test fixtures can't easily reach the
    executor.py:255 path without spinning up a real approval flow.

Start by reading handbook.md fully, then this phase doc, then doing
the two pre-coding greps (channel adapter location and eval scorer
location). Report findings before writing code.
```

---

## Scope

| Step | What | Where |
|---|---|---|
| 7 | WebUI admin + capacity advisory + conv list filter + approval parent-walk + **channel-level approval fan-out** | `src/webui/schemas.py`, `src/webui/admin.py`, the channel adapter (location confirmed by grep), `web/` (frontend chip rendering) |
| 8 | Routing-accuracy eval scorer + 50-case dataset + nightly | `src/rolemesh/evaluation/scorers/routing_accuracy.py`, `tests/data/routing_dataset.jsonl`, CI config |
| 9 | Documentation update | `docs/frontdesk-architecture.md`, `README.md` |

See `handbook.md` §6 Step 7/8/9 for full code-level specs.

---

## Commit plan

### Commit 7 — `feat(frontdesk): webui admin + approval parent-walk + fan-out`

The biggest commit in Phase C. ~390 prod + ~280 tests.

**Three sub-pieces, all in one commit because they're tightly
coupled by the "frontdesk is visible to admin + users" UX surface:**

1. **WebUI admin** — `is_frontdesk` toggle (only when
   `agent_role='super_agent'`), `routing_description` textarea (only
   for domain agents). Capacity advisory using the formula in
   handbook §4 decision #21. Returns 400 if `is_frontdesk=True` but
   `agent_role != 'super_agent'`.

2. **User conversation list + approval parent-walk** — The
   conversation list endpoint passes `include_children=False` (Phase A
   loader default already covers this; verify the endpoint actually
   uses the default). Approval list endpoint adds the parent-walk SQL
   from handbook §6 Step 7.4.

3. **Channel-level approval fan-out** — this is THE correction over
   v1. Locate `MessageChannel.send_to_conversation` (grep — likely in
   `src/rolemesh/main.py` near the orchestrator's `_channel` builder
   and/or in `src/rolemesh/approval/notification.py`). Modify to:
   - After dispatching to `conversation_id`, fetch the conversation.
   - If `conv.parent_conversation_id` is non-null, also dispatch to
     the parent with extra metadata
     `{source: "delegation_fanout", via_target_coworker_id,
     via_target_name}`.
   - Frontend `message-item` renders the chip.

**Tests in Commit 7**:

- `tests/webui/test_admin_frontdesk.py`:
  - `is_frontdesk=True` requires `super_agent`.
  - `routing_description` editable.
  - Capacity advisory returns warning, doesn't block.
  - Conversation list returns no child convs by default.
  - Approval parent-walk returns child-conv approvals to parent
    viewer.

- `tests/integration/test_approval_fanout.py`:
  - Approved + executed report (`executor.py:255` path) → both child
    and parent get the message; parent metadata contains
    `via_target_name`.
  - Rejected notice (`executor.py:204`) → both rows get rejection.
  - Skipped notice (`engine._send_to_origin`) → both rows.
  - Non-regression: plain conversation (parent_conversation_id
    NULL) → only the conv itself; no fan-out row.
  - Repeat sends don't dedupe: two `send_to_conversation(child, ...)`
    produce two fan-out rows.

### Commit 8 — `feat(frontdesk): routing-accuracy eval scorer + dataset`

~150 prod + ~70 tests + dataset.

1. **Pre-check grep**:
   ```bash
   grep -rn "tool_use\|trace\|tool_call" \
     src/rolemesh/evaluation/scorers/ 2>/dev/null
   ```
   If a "read trace.tool_use args" template exists → reuse, ~80
   lines of scorer code. If not → write from scratch, ~150 lines.
   **Record the finding in the commit message.**

2. **Dataset** `tests/data/routing_dataset.jsonl`:
   - ≥ 50 cases (v1.2 launch floor).
   - ≥ 20% adversarial.
   - Each routing target has ≥ 5 cases.
   - 5-10 no-match cases (`expected_target=null`).
   - At least 5 cases for the failure-passthrough contract:
     target returns safety_blocked → verify frontdesk reply contains
     specialist name + literal reason.
   - **Document the 3-month growth plan** from handbook §6 Step 8.2 in
     the commit message and in `docs/frontdesk-architecture.md`: grow
     to ≥ 150 cases + ≥ 30 adversarial within 3 months of ship, mined
     from real routing mistakes in the `delegations` audit table. A
     release-blocking gate that never grows past 50 becomes a rubber
     stamp.

3. **Scorer rules** (handbook §6 Step 8.3).

4. **Nightly wiring**: this is release-blocking. Add to CI nightly
   config. Document in commit message what the gate criterion is
   (e.g., "score ≥ 0.85 on the dataset").

### Commit 9 — `feat(frontdesk): docs to v1.2`

~500 lines of markdown.

Update `docs/frontdesk-architecture.md` from v1 to v1.2:

- Motivation, architecture (parent/child ASCII diagram).
- Data model: `parent_conversation_id`, `internal` binding,
  `delegations` table. **Explicit explanation of route B vs route A**
  (why we don't reuse parent's conversation_id).
- Tool contracts: `list_agents`, `delegate_to_agent`.
- Synchronous + parallel semantics.
- Safety / approval / OIDC. Async approval explicitly.
- **Approval UI parent-walk + outcome fan-out (both paths) with the
  channel-adapter rationale.**
- All 13 known v1 trade-offs from handbook §9 listed verbatim
  (including #13 sticky-mode cold-start latency — explain the
  contrast with `task_scheduler.py:_TASK_CLOSE_DELAY_S` and why v1
  intentionally does not replicate the warm-keep delay).

Add one line to `README.md` Features section: "Frontdesk: single
user-facing entry point per tenant that delegates to specialist
agents."

---

## Pre-coding checklist

Run these greps before commit 7:

```bash
# A. Channel adapter location
grep -rn "def send_to_conversation\|async def send_to_conversation" \
  src/rolemesh/ --include='*.py'

# Expect to find:
#   - one or more class methods (likely in main.py and/or
#     approval/notification.py)
# Decision: which one is the actual primary path used by the
# orchestrator? If unclear, ask the user.

# B. Eval scorer pattern
grep -rn "class.*Scorer\|def score" \
  src/rolemesh/evaluation/scorers/ 2>/dev/null

# C. Frontend message-item component
grep -rn "message-item\|MessageItem\|delegation_fanout" \
  web/ 2>/dev/null
```

Report findings before writing the fan-out code. If the channel
adapter has multiple implementations (e.g. one for WS, one for NATS
pub) and they don't share a base class, **stop and ask** — the
fan-out logic might need to be in a different layer.

---

## Risks specific to Phase C

1. **Channel adapter has multiple implementations**. If
   `send_to_conversation` exists in both `main.py` and
   `approval/notification.py` with different bodies, the fan-out
   needs to either go in both or in a shared base. Ask the user
   rather than guessing.

2. **`get_conversation`/`get_coworker` calls inside
   `send_to_conversation`** add a DB round-trip per outgoing message.
   For a hot path (every approval write-back), measure if a cache
   is needed. For v1, accept the cost; v1.5 can cache.

3. **The dataset**. 50 cases with 20% adversarial is work — about an
   hour to author. Make it realistic, not synthetic. If the session
   can't reach 50 good cases, stop and ask for input.

4. **Nightly wiring** depends on the project's CI setup. If
   `rolemesh-eval` doesn't have a nightly job slot, document the
   integration as a follow-up and gate locally for now (run as part
   of pytest with a `pytest.mark.eval` marker).

5. **Docs drift**. `docs/frontdesk-architecture.md` exists as v1.
   Replace, don't append. Make sure the "known v1 trade-offs"
   section matches handbook §9 word-for-word (13 items, including
   the sticky cold-start trade-off added during the v1.2 review).

---

## Definition of done

- [ ] 3 commits on `feat/frontdesk`, ordered 7 → 8 → 9.
- [ ] `uv run pytest && uv run mypy src && uv run ruff check src tests`
      green after each commit.
- [ ] `git commit -s`, prefix `feat(frontdesk):`.
- [ ] Approval fan-out tests pass for all 4 paths (executed,
      rejected, skipped, non-regression). Parent metadata contains
      `via_target_name`.
- [ ] WebUI admin: `is_frontdesk=True` requires `super_agent`;
      capacity advisory is non-blocking.
- [ ] Conversation list endpoint returns no child convs.
- [ ] Approval list endpoint includes child-conv approvals via
      parent-walk.
- [ ] Routing-accuracy eval scorer passes ≥ 0.85 on the dataset
      (or whatever threshold the team agrees on).
- [ ] Eval wired into nightly (or documented as follow-up if CI
      slot is unavailable).
- [ ] `docs/frontdesk-architecture.md` is v1.2 and matches handbook
      §9 trade-off list (13 items) word-for-word.
- [ ] Dataset growth plan (≥150/≥30 in 3 months) is mentioned in
      both Commit 8 message and `docs/frontdesk-architecture.md`.
- [ ] `README.md` Features mentions frontdesk.

After Phase C, the feature is production-ready and the branch is
ready for PR review (`gh pr create` — but only when the user
explicitly asks).

---

## After Phase C

Once Phase C is in:

1. Manual smoke test on a dev orchestrator:
   - Create a frontdesk + two domain specialists in one tenant.
   - Talk to frontdesk via the WebUI; verify routing chips appear.
   - Trigger an approval flow inside a delegated call; verify the
     fan-out chip appears in the parent conversation.
   - Run the routing eval locally.

2. Update `memory/project_frontdesk_v1.md` to mark v1.2 as shipped
   (status flip + reference the merged PR# once it exists).

3. Open the PR. The PR description should reference
   `docs/frontdesk-impl/handbook.md` as the design spec and list the
   9 commits as the implementation log.

The user owns deciding when to open the PR. Do not do it
automatically.
