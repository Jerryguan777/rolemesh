# Phase A — Foundation (Step 1, 2, 3)

> Branch: `feat/frontdesk` · Estimated session length: ~600 LOC
> (60 schema + 160 prod + 380 tests).
> Output: 3 commits on `feat/frontdesk`. Not pushed.

---

## Session Prompt

> Paste the block below as the first message in a fresh Claude Code
> session. It is self-contained — the new session does not need to
> see this chat history.

```
You are implementing Phase A (foundation) of Frontdesk Agent v1.2 in
the RoleMesh codebase. Working directory: /home/jerry/ai/rolemesh-3.
Branch: feat/frontdesk.

REQUIRED READING ORDER:
1. docs/frontdesk-impl/handbook.md  — the full design + 9-step plan +
   verified facts + 35 pitfalls. The source of truth.
2. docs/frontdesk-impl/phase-a-foundation.md  — this phase's scope.

Phase A covers Steps 1, 2, 3 only:
  Step 1: DB schema migration (parent_conversation_id column,
          is_frontdesk + routing_description columns, delegations
          table with RLS).
  Step 2: DB helpers + dataclass extensions + loader updates +
          _coworker_from_state fix.
  Step 3: ToolContext gets nc (NATS client) and role_config fields,
          plus a request() helper for core NATS request-reply.

CRITICAL: Step 2 includes a grep-based audit of `_state.coworkers`
access points and conversation list queries. The grep output and
your per-line conclusions go in the Step 2 commit message. This
audit is the most important deliverable of Step 2 — do not skip it.

CRITICAL: The _coworker_from_state fix (Step 2.3) is a real latent
bug — current implementation drops 6 of 14 fields including
permissions and agent_role. Read handbook §3 fact #22 and §6 Step
2.3 carefully.

Work commit-by-commit. After each commit:
  uv run pytest && uv run mypy src && uv run ruff check src tests
must all pass. Use `git commit -s` (sign off). Commit prefix:
`feat(frontdesk):`. Do not push, do not open a PR.

Stop and ask when:
  - Existing code conflicts with what handbook §3 (verified facts)
    describes.
  - Grep reveals access points not anticipated by the audit.
  - mypy / ruff errors that aren't resolvable without # type: ignore
    or # noqa.
  - Tests fail because of your change (don't modify the tests to fit).

Start by reading handbook.md fully, then this phase doc, then run
the grep audit commands (Step 2.5). Report back what the grep
output looks like before writing any code.
```

---

## Scope

| Step | What | Where |
|---|---|---|
| 1 | Schema migration: `parent_conversation_id`, `is_frontdesk`, `routing_description`, `delegations` table | `src/rolemesh/db/schema.py` |
| 2 | DB helpers + dataclass fields + loader `include_children` + `_coworker_from_state` fix | `src/rolemesh/db/delegation.py` (new), `src/rolemesh/db/chat.py`, `src/rolemesh/db/coworker.py`, `src/rolemesh/core/types.py`, `src/rolemesh/main.py` |
| 3 | `ToolContext` adds `nc` and `role_config` fields + `request()` helper | `src/agent_runner/tools/context.py`, `src/agent_runner/main.py` |

See `handbook.md` §6 (Step 1, 2, 3) for the full code-level spec.

---

## Commit plan

Three commits, in this order:

1. **`feat(frontdesk): add schema columns + delegations table`**
   - Only schema.py changes.
   - Run `bootstrap()` test if one exists; otherwise add a smoke test
     that `CREATE TABLE delegations` succeeds and the new columns are
     visible.

2. **`feat(frontdesk): db helpers + dataclass + _coworker_from_state fix`**
   - `db/delegation.py` new module.
   - `Coworker` and `Conversation` dataclasses get the new fields.
   - `_coworker_from_state` simplified to `return cw_state.config`.
   - List queries get `include_children=False` default.
   - Commit message body: include `/tmp/audit_state_access.txt` and
     `/tmp/audit_conv_queries.txt` outputs **with per-line
     annotations**.

3. **`feat(frontdesk): ToolContext nc + role_config + request() helper`**
   - `ToolContext` dataclass updated.
   - `agent_runner/main.py` construction site passes `nc` and
     `role_config=dict(init_data.role_config or {})`.
   - Tests for None→{} normalization and shallow-copy isolation.

If any commit can't pass `pytest + mypy + ruff`, fix in-place — do
NOT commit and then commit a fix.

---

## Verification before commit 2

The `_coworker_from_state` fix changes a function with 2 callers
(`main.py:1330` and `main.py:1646` at time of writing). Verify the
callers still type-check correctly after the change — `cw_state.config`
returns the full `Coworker`, the partial version returned a
synthesized `Coworker`; both have the same type signature so there
should be no caller-side change, but check mypy explicitly.

Also: the grep audit will surface code that reads
`cw_state.conversations` or `_state.coworkers[id].conversations`.
For each such site, decide whether it assumes "all entries are user
conversations." If yes, document why the post-Phase-A loader default
(`include_children=False`) keeps the assumption intact.

---

## Tests added in Phase A

| File | What |
|---|---|
| `tests/db/test_delegation.py` | DB helpers — idempotent binding, find with chat_id filter, ON CONFLICT create, conditional terminal UPDATE, cleanup_running_delegations |
| `tests/core/test_coworker_from_state_full_copy.py` | Builds a fully-populated Coworker (all 14 fields including `is_frontdesk=True`, `permissions`, `agent_role`, `status`, `container_config`), wraps in `CoworkerState`, calls `_coworker_from_state(cs)`, asserts every field round-trips |
| `tests/agent_runner/test_tool_context.py` | `request()` happy + timeout, role_config None→{}, role_config shallow-copy isolation |

Phase B's tests come later and depend on these being in place.

---

## Out of Phase A scope

Things that look related but belong to later phases:

- `delegate_to_agent` tool implementation → Phase B Step 5.
- `list_agents` tool implementation → Phase B Step 4.
- Frontdesk catalog injection at spawn → Phase B Step 6.
- WebUI `is_frontdesk` toggle and `routing_description` textarea →
  Phase C Step 7.
- Approval outcome fan-out at the channel adapter → Phase C Step 7.6.
- Documentation update of `docs/frontdesk-architecture.md` → Phase C
  Step 9. (Phase A may leave the existing v1 doc stale; that's fine.)

---

## Definition of done

- [ ] 3 commits on `feat/frontdesk`, in order.
- [ ] `uv run pytest && uv run mypy src && uv run ruff check src tests`
      green after each commit.
- [ ] `git log feat/frontdesk` shows the 3 commits with `feat(frontdesk):`
      prefix and `-s` sign-off.
- [ ] `git diff main feat/frontdesk -- src/rolemesh/db/schema.py` shows
      the 4 schema additions cleanly.
- [ ] Step 2 commit message contains the grep audit output and
      annotations.
- [ ] `_coworker_from_state` is now `return cw_state.config` and the
      tests in `test_coworker_from_state_full_copy.py` pass.
- [ ] No tests modified to fit code (the constraint, not a checklist
      item the session can self-verify — humans verify on review).

After Phase A, the repo is in a green, mergeable, lower-tech-debt
state regardless of whether Phase B happens. That's the goal.
