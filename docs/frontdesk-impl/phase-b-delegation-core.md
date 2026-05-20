# Phase B — Delegation core (Step 4, 5, 6)

> Branch: `feat/frontdesk` · Estimated session length: ~2,180 LOC
> (630 prod + 1,550 tests). **The heaviest phase. One dedicated session.**
> Output: 3 commits on `feat/frontdesk`. Not pushed.
> Depends on: Phase A merged into `feat/frontdesk` (commits done).

---

## Session Prompt

> Paste the block below as the first message in a fresh Claude Code
> session. It is self-contained.

```
You are implementing Phase B (delegation core) of Frontdesk Agent v1.2
in the RoleMesh codebase. Working directory: /home/jerry/ai/rolemesh-3.
Branch: feat/frontdesk. Phase A is already complete (3 commits on
this branch). Verify with `git log --oneline -10`.

REQUIRED READING ORDER:
1. docs/frontdesk-impl/handbook.md  — source of truth. Especially §3
   (verified facts), §4 (decisions), §6 Step 4-6, §8 (35 pitfalls).
2. docs/frontdesk-impl/phase-b-delegation-core.md  — this phase doc.

Phase B covers Steps 4, 5, 6 only:
  Step 4: list_agents tool + orchestrator handler + catalog rendering
          (orchestration/catalog.py with FRONTDESK_RULES).
  Step 5: delegate_to_agent tool + delegation handler
          (orchestration/delegation.py). This is the CORE.
  Step 6: Frontdesk catalog injection at agent spawn time.

This is roughly half the total feature LOC. ~1,300 lines of that is
tests — the 22-scenario matrix in handbook §6 Step 5.5.

NON-NEGOTIABLE CONTRACTS you must internalize before writing code:

  (a) Pi backend success is TWO events: text-bearing (is_final=False)
      + marker (is_final=True, result=None). _on_output must track
      both and merge in _merge_pi_two_event_pattern.

  (b) The target container does NOT exit on its own. Every terminal
      path in _on_output (success-marker, error, safety_blocked,
      stopped) AND in the closure's TimeoutError branch must
      explicitly call queue.request_shutdown(queue_key).

  (c) OUTER_GUARD_S (30s) is for "the closure never ran" only. The
      business deadline (300s for slow LLMs) lives INSIDE the closure
      as wait_for(execute, 300). These two timers do NOT stack. They
      produce DISTINCT audit messages so ops can tell them apart.

  (d) Sticky session persistence: handler must EXPLICITLY call
      set_session(child_conv.id, ..., new_session_id) after success.
      _run_agent's set_session does NOT cover delegation (it's a
      sidepath). DB failure here is non-fatal — log a warning and
      proceed.

  (e) Catalog renders "(id: xxx)" NOT "(folder: xxx)". FRONTDESK_RULES
      uses "agent id" NOT "folder slug". This is to keep the LLM from
      treating specialists as filesystem paths (real observed bug
      because frontdesk inherits super_agent bash perms).

  (f) MAX_DELEGATION_DEPTH = 1. depth=0 in payload → allowed
      (frontdesk's initial call). depth=1 → rejected. Strictly 1 hop.

  (g) sticky concurrent calls to same (parent, target): use
      INSERT ... ON CONFLICT DO NOTHING RETURNING + fallback SELECT
      in create_child_conversation. Two concurrent sticky calls
      must converge to one child conv row and one shared session.

  (h) GroupQueue _shutting_down=True: handler must check BEFORE
      enqueue_task, write audit error, and early-return. Do NOT
      enqueue then try to recover.

Work commit-by-commit:
  Commit 4 — list_agents
  Commit 5 — delegate_to_agent (the big one)
  Commit 6 — frontdesk catalog injection at spawn

After each commit:
  uv run pytest && uv run mypy src && uv run ruff check src tests
must pass. Use `git commit -s`. Prefix `feat(frontdesk):`. Do not
push.

Stop and ask when:
  - Any of contracts (a)-(h) above can't be satisfied as designed.
  - Tests reveal a fact in handbook §3 was wrong.
  - Test #17 (role_config NATS interception) — the AgentInitData
    subject/KV key isn't where handbook describes. Grep first;
    report the actual location before changing the test design.
  - You hit an mypy / ruff error not resolvable without # type:
    ignore.

Suggested workflow:
  1. Read handbook.md fully.
  2. Read phase-b-delegation-core.md.
  3. Open a TaskCreate task list with the 22 test scenarios from
     handbook §6 Step 5.5 — keep them visible the whole session.
  4. Step 4 first (smaller, gives infrastructure for Step 5 tests).
  5. Step 5: write delegation.py skeleton with all helper signatures
     and the merge + OUTER_GUARD try/except shapes BEFORE filling in
     bodies. The shape is where the contracts live.
  6. Step 5 tests in numerical order from the matrix; each one is
     ~60-80 lines with testcontainers/NATS setup.
  7. Step 6 last — small, depends on Steps 4-5 catalog code.

Start by confirming Phase A is in (git log) and reading both docs.
Report your reading + the TaskCreate plan before writing code.
```

---

## Scope

| Step | What | Where |
|---|---|---|
| 4 | `list_agents` tool + orchestrator handler + `catalog.py` with `render_agent_catalog` and `FRONTDESK_RULES` | `src/agent_runner/tools/rolemesh_tools.py`, new `src/rolemesh/orchestration/catalog.py`, `src/rolemesh/main.py` (subscription) |
| 5 | `delegate_to_agent` tool + orchestrator `delegation.py` handler + defensive `send_message` guard | `src/agent_runner/tools/rolemesh_tools.py`, new `src/rolemesh/orchestration/delegation.py`, `src/rolemesh/main.py` (subscription + startup ordering) |
| 6 | Frontdesk catalog injection at `AgentInitData(...)` construction | `src/rolemesh/agent/container_executor.py` (or wherever `AgentInitData` is constructed) |

See `handbook.md` §6 Step 4/5/6 for full code-level specs.

---

## Commit plan

Three commits, in this order. Each builds on the previous.

### Commit 4 — `feat(frontdesk): list_agents tool + catalog renderer`

- New file `src/rolemesh/orchestration/catalog.py` with
  `render_agent_catalog()` and `FRONTDESK_RULES` constant.
- New tool `list_agents` in `agent_runner/tools/rolemesh_tools.py`.
- Subscription `agent.*.list_agents.request` in `main.py`.
- Tests:
  - `tests/orchestration/test_catalog_no_filesystem_terms.py` — asserts
    `(id:` is present, `folder`/`directory` are absent in both
    catalog body and FRONTDESK_RULES.
  - `tests/integration/test_list_agents.py` — active, paused,
    cross-tenant, self-exclusion, frontdesk-exclusion.

### Commit 5 — `feat(frontdesk): delegate_to_agent tool + delegation handler`

The big one. ~440 prod + ~1300 tests.

- New file `src/rolemesh/orchestration/delegation.py` with
  `handle_delegate_request`, `_process_one`,
  `_merge_pi_two_event_pattern`, `_resolve_target`,
  `_map_output_to_response`, `_err`, `_timeout_response`,
  `_error_response`, and the constants `MAX_DELEGATION_DEPTH=1`,
  `DEFAULT_BUSINESS_DEADLINE_S=300.0`, `OUTER_GUARD_S=30.0`.
- New tool `delegate_to_agent` in `agent_runner/tools/rolemesh_tools.py`.
- Defensive guard added to `send_message`: refuses if
  `ctx.role_config.get("is_delegated_call")` is true.
- `main.py` subscriptions for `agent.*.delegate.request`, with the
  startup order: `cleanup_running_delegations()` → `_load_state_from_db()`
  → NATS subscribes → `_message_loop`.
- Tests: the 22-scenario matrix below in §"Test matrix detail".

### Commit 6 — `feat(frontdesk): inject catalog + FRONTDESK_RULES on frontdesk spawn`

- In `container_executor.py` (or `AgentInitData` construction), when
  `coworker.is_frontdesk`, append the rendered catalog +
  FRONTDESK_RULES to `system_prompt`.
- Tests:
  - `tests/integration/test_frontdesk_spawn.py` — `is_frontdesk=False`
    leaves prompt alone; `is_frontdesk=True` appends catalog +
    FRONTDESK_RULES; empty catalog case; **A6 regression**:
    `executor.get_coworker(target_id)` returns Coworker with
    `is_frontdesk == True`, `permissions` correct (this catches any
    accidental regression of the Phase A `_coworker_from_state` fix).

---

## Test matrix detail (22 scenarios — Step 5)

Open a TaskCreate task list at session start with these 22 entries.
Mark in_progress while writing each; completed when its test passes.

| # | Name | Critical assertion |
|---:|---|---|
| 1 | happy path | child conv created with `parent_conversation_id`, audit `success`, reply reaches frontdesk |
| 2 | permission rejected | both agent-side and orchestrator-side gates fire |
| 3 | self-delegation rejected | handler returns error |
| 4 | cross-tenant rejected | handler returns "Tenant mismatch" |
| 5 | target not found | error text contains catalog |
| 6 | depth limit | `depth=0` allowed, `depth=1` rejected |
| 7 | target is frontdesk / super_agent | both rejected by `_resolve_target` |
| 8 | isolated | `session_id=None`; each call creates new child conv |
| 9 | sticky round-trip | 1st: handler **explicit** `set_session(child, S1)`; assert `get_session(child)==S1` AND `get_session(parent)==BEFORE_DELEGATE`; 2nd: same child reused, `resume=S1` flows in. Plus A3: isolated-then-sticky must NOT reuse |
| 10 | safety_blocked passthrough | isError=true, reason text intact |
| 11 | business deadline | target sleeps 400s; inner `wait_for(300)` trips; audit `status='timeout'`, message contains `"took too long"` |
| 12 | audit idempotency | write `timeout` then try write `success`; second call returns False; DB row stays `timeout` |
| 13 | multi-reply target merge (A2) | Two events: `(success, is_final=False, result='Hi')` + `(success, is_final=True, result=None, new_session_id='S')`. Merged response text = `'Hi'`, new_session_id = `'S'` |
| 14 | parallel delegation | one turn, two delegates to different targets, both complete concurrently |
| 15 | OIDC pass-through | stub MCP receiver asserts `X-RoleMesh-User-Id` = parent's user_id |
| 16 | `send_message` blocked in delegated | refusal with `is_error=True` |
| 17 | role_config isolation (A) | unit test: `_build_agent_input(...)` returns `AgentInput.role_config == {is_delegated_call, delegated_by, delegation_depth=1, parent_conversation_id, delegation_id}` EXACTLY |
| 17 | role_config isolation (B) | integration: fixture subscribes to the `AgentInitData` write path (grep `AgentInitData` to locate); real delegation → captured init_data.role_config matches A's dict |
| 18 | GroupQueue shutting down | `queue._shutting_down=True`. No `enqueue_task` call. Error response. Audit row `status='error'`, message `'GroupQueue is shutting down; delegation refused.'` |
| 19 | child conv NOT in `_state.coworkers` | run `_message_loop` one tick; assert child conv id absent from `_state.coworkers[target].config.conversations` |
| 20 | sticky concurrency race | `asyncio.gather` two concurrent sticky calls to same (parent, target). Exactly one child conv row. Both reuse the same session. |
| 21 | explicit request_shutdown (A1) | Mock `queue.request_shutdown` with counter. Assert it was called on each terminal path: success, error, safety_blocked, business timeout. Also assert `result_future.get_loop() is asyncio.get_running_loop()` |
| 22 | OUTER_GUARD vs business timeout — distinct audit | Variant a: monkey-patch `queue.enqueue_task` to no-op → OUTER_GUARD fires → audit `status='error'`, message `'Delegation task never started (queue stalled).'`. Variant b: closure runs but `wait_for(300)` trips → audit `status='timeout'`. **The two error_message strings MUST differ.** |

Total ≈ 1300 lines of test code.

---

## Pre-coding checklist for Commit 5

Before writing `delegation.py` body:

1. Sketch the skeleton: file structure, constants, function signatures
   for `_process_one`, `_on_output`, `_closure`, `_merge_pi_two_event_pattern`,
   `_resolve_target`, response builders. **Save as a separate first
   draft pass.**
2. In `_process_one`, draw the try/except/except structure that
   distinguishes OUTER_GUARD vs business timeout — handbook §6 Step
   5.3 step 9.
3. Verify the merge function handles all three input shapes:
   (text, marker, None), (None, None, terminal), (text, None, None).
4. Add `# Pi two-event` and `# OUTER_GUARD vs business timeout`
   comments to lock the contracts into the file.

Then start filling in bodies and tests.

---

## Pre-coding checklist for Commit 6

Phase A must be in (`_coworker_from_state` returns the full config).
Grep `AgentInitData(` to confirm the construction site:

```bash
grep -rn "AgentInitData(" src/rolemesh/ --include='*.py'
```

The frontdesk-injection branch goes right where `system_prompt` is
assembled. Do NOT inject anywhere else.

---

## Risks specific to Phase B

1. **Pi backend two-event success** is the highest-risk contract. If
   `_on_output` matches only `is_final=True`, the response text is
   empty in production. Test #13 catches this.

2. **request_shutdown** missing on any one terminal path → that path
   hangs until `CONTAINER_TIMEOUT` (30 min). Test #21 enforces all
   paths.

3. **Test #17 (B)** depends on intercepting `AgentInitData` over NATS.
   If the actual mechanism differs from handbook §6 Step 5.5 #17
   (e.g. AgentInitData flows over a JetStream KV bucket rather than a
   subject), the test design needs adjustment. **Grep first; report
   actual location** before adapting the test.

4. **Sticky concurrency race (test #20)** uses `INSERT ... ON CONFLICT
   DO NOTHING RETURNING` + fallback SELECT. If the fallback SELECT is
   missing, the second concurrent caller gets None and the handler
   silently fails. This is a real database concurrency hazard.

5. **`get_executor(target_co.agent_backend)`** may return None for an
   unknown backend. The handler has an explicit None check that emits
   error response + audit error. Do not skip this branch.

---

## Definition of done

- [ ] 3 commits on `feat/frontdesk`, ordered 4 → 5 → 6.
- [ ] `uv run pytest && uv run mypy src && uv run ruff check src tests`
      green after each commit.
- [ ] `git commit -s`, prefix `feat(frontdesk):`.
- [ ] 22 tests pass. (Look at TaskList in the session to confirm
      every scenario got its test.)
- [ ] `_merge_pi_two_event_pattern` handles all three input shapes
      explicitly.
- [ ] `queue.request_shutdown(queue_key)` is called on EVERY terminal
      path. Verified by test #21.
- [ ] OUTER_GUARD vs business timeout produce distinct
      `error_message` text. Verified by test #22.
- [ ] Sticky concurrency test (#20) passes — race converges to one
      child conv.
- [ ] No tests modified to fit code.
- [ ] No `# type: ignore` or `# noqa` added without explicit user
      OK.

After Phase B, frontdesk works end-to-end (CLI test possible). UI
plumbing and approval-fan-out come in Phase C.
