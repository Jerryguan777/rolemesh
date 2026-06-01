# HITL Approval — S5 handoff notes (policy CRUD + isolation + docs)

Session S5 of `docs/21-hitl-approval-plan.md` (§10 S5). Built on S1's frozen
contract, the S2 hook, the S3 coordinator, and the S4 delivery layer; no
contract drift. Branch: `feat/hitl-approval-B`. This is the final session — the
plan's §11/§12 now consolidate the cross-session findings, and a full
`docs/21-hitl-approval-plan-cn.md` mirror ships alongside.

## What S5 landed

### Backend — policy CRUD REST + pending read
- **`src/agent_runner/approval/policy.py`** — added `validate_condition_expr`
  (+ `ConditionValidationError`): the strict, **write-time** companion to the
  lenient, fail-closed `evaluate_condition`. It accepts only the §7 grammar with
  exactly the keys of one form per node (a mixed `{always:true, field:…}` node is
  rejected, even though the runtime evaluator would silently take the first
  branch), and bounds nesting depth. Rationale: the *gate* stays fail-closed; the
  *editor* stays strict, so an operator fixes a typo up front instead of shipping
  a policy that approval-gates everything.
- **`src/webui/v1/approvals.py`** (new) — two routers:
  - `/api/v1/approval-policies` — list / create / get / patch / delete over the
    S1 `db/approval.py` helpers. Tenant-scoped (RLS + explicit `WHERE tenant_id`).
    A bad-UUID is caught (`asyncpg.DataError`) and collapses to the same 404 a
    valid-but-absent id gets — no uuid-shape oracle.
  - `/api/v1/approval-requests` — pending read for web reconnect (optional
    `conversation_id` filter, applied **inside** the tenant-scoped read). The
    projection (`PendingApprovalRequest`) exposes the tool name + summary, never
    the raw params.
- **`src/webui/schemas_v1.py`** — `ApprovalPolicy` / `ApprovalPolicyCreate` /
  `ApprovalPolicyUpdate` / `PendingApprovalRequest`. The two write bodies run
  `condition_expr` through a `field_validator` that delegates to
  `validate_condition_expr` → 422 on a malformed expression.
- **`src/webui/api_v1.py`** — registered both routers.
- **OpenAPI + TS** — added the three endpoints + `ConditionExpr` / `ApprovalPolicy`
  / `ApprovalPolicyCreate` / `ApprovalPolicyUpdate` / `PendingApprovalRequest`
  schemas + the `ApprovalPolicies` tag to `contracts/openapi.yaml`; regenerated
  `web/src/api/generated/types.ts`. Codegen-freshness + contract suites green.

### Frontend — approval card + reconnect + policy CRUD UI
- **`web/src/components/approval-store.ts`** (new, pure) — `upsertRequested` /
  `applyResolved` / `mergePending`. Idempotent on `request_id`; a redelivered
  `requested` event never reverts a resolved card to pending; `mergePending`
  drops a pending card decided while disconnected and keeps resolved cards.
- **`web/src/components/approval-card.ts`** (new) — `rm-approval-card`: action
  summary + ✅/❌, emits `approval-decision`; shows a terminal status pill once
  resolved (buttons gone). Carries only the request id + verb.
- **`web/src/components/chat-panel.ts`** — handles `event.approval.requested` /
  `event.approval.resolved`, relays a tap via `sendApprovalDecision` (busy-guards
  a double-tap), and on (re)connect re-renders in-flight cards from
  `listPendingApprovals(conversationId)`.
- **`web/src/components/condition-form.ts`** (new, pure) — `buildConditionExpr` /
  `exprToForm` / `parseValue`: the structured form ⇄ `condition_expr` mapping.
  Exposes the shallow §7 subset (`{always}` or a flat and/or of leaves); a deeper
  stored expression reports `editable:false` so the page renders it read-only
  rather than silently flattening it.
- **`web/src/components/approval-policy-dialog.ts`** + **`approval-policies-page.ts`**
  (new) — `rm-approval-policies-page` under Settings → Governance: list + the
  create/edit dialog with the condition builder + delete confirm. A complex
  stored condition opens read-only and is omitted from the PATCH body (left
  untouched).
- **`web/src/api/client.ts`** — `listApprovalPolicies` / `createApprovalPolicy` /
  `updateApprovalPolicy` / `deleteApprovalPolicy` / `listPendingApprovals`.
- **`web/src/components/settings-shell.ts`** — registered the page nav entry.

## Cross-tenant attack-sim (the S5 exit criterion)

`tests/webui/test_v1_approval_policies.py` (17, testcontainers Postgres) proves
the REST layer above the S1 DB-layer RLS/WHERE belts:
- tenant-A user wielding a tenant-B policy id → 404 on get/patch/delete, and the
  victim row is verified unchanged afterward (no silent write);
- list never leaks another tenant's policies;
- the pending read is tenant-scoped even under a foreign `conversation_id`;
- a hostile body cannot smuggle `tenant_id`/`id` (`extra="forbid"` → 422);
- bad-UUID and absent-UUID collapse to the same 404.

Plus CRUD round-trip, default-condition, and malformed-condition (422) coverage.

## Tests (all green)

- Python: `tests/webui/test_v1_approval_policies.py` (17),
  `tests/test_openapi_contract.py` + `tests/test_openapi_codegen_freshness.py`
  (36). Prior approval suites (S1–S4) unaffected.
- Web: full `vitest run` green (339). New: `approval-store.test.ts` (12),
  `approval-card.test.ts` (7), `condition-form.test.ts` (19),
  `approval-policies-page.test.ts` (9 — page + dialog + `summarizeCondition`).
  `settings-shell.test.ts` nav-count fixture bumped 10 → 11.
- Web lints (`tokens-only`, `flat-route`, `no-admin-chat`) clean.

## Known limitations / pre-existing items (not introduced here)

- `web tsc --noEmit` reports 5 errors, all **pre-existing and unrelated to HITL**
  (`chat-shell.test.ts` stale literal fixtures ×3, `chat-shell.ts`
  `import.meta.env`, `skill-dialog.test.ts` cast). Every HITL file is tsc-clean.
  Left for a separate cleanup — out of S5 scope, and the repo's CI gate is vitest
  (there is no `tsc` npm script).
- Telegram/Web live click-through (S4 note) is still the one manual pass worth
  doing before merge; the wire contracts are pinned and automated.
- `cleanup_orphans` vs surviving containers (§11.3) remains the documented ops
  item.
