# Row-Level Security Architecture

This document explains how RoleMesh enforces multi-tenant isolation at the PostgreSQL layer using Row-Level Security (RLS) — the reasoning behind moving the trust boundary into the database, the alternatives considered, and the dual-pool / four-function-class architecture that makes RLS coexist with legitimate cross-tenant maintenance work.

It is the security-architecture companion to [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md). Read that first if you need the tenant data model; this document assumes that context.

Target audience: developers adding new tenant-scoped tables, debugging "user sees empty results" symptoms, integrating new background loops that touch multiple tenants, or reviewing whether a new code path correctly respects tenant boundaries.

---

## Background: Why Application-Layer Filtering Is Not Enough

RoleMesh's multi-tenant model stores all tenant data in a shared schema, keyed by a `tenant_id UUID` column on every business table. The earliest version of the system relied on application code to write `WHERE tenant_id = $1` in every query. A subsequent refactor (the "tenant-scope all by-id lookups" change) made `tenant_id` a required keyword argument on every by-id DB function, so the language itself rejects calls that forget to pass it.

That refactor closed one entire class of bugs — the forgotten `WHERE` clause — but it left the trust boundary in application code. Four classes of failure remain:

- **SQL injection** in any one endpoint bypasses the entire model.
- **A new query path** added without following the pattern leaks silently; review discipline is the only guard.
- **Raw `psql` sessions** by operators have full cross-tenant visibility, with no audit on what they read.
- **Trigger-derived columns** (like `safety_rules_audit.tenant_id`) can drift if the trigger is disabled or a new write path bypasses it.

RLS is the answer to all four: the database itself rejects the query, regardless of which application bug, operator mistake, or schema drift produced it. After RLS, application-layer tenant filters become **defense in depth** — useful but no longer the primary guard.

The non-obvious cost of RLS is that the database now needs to know "which tenant is this query for?" That context has to come from somewhere on every query. The bulk of this document is about how that context flows.

---

## Design Goals

1. **Three layers of defense.** Application parameter (a function argument), connection context (a Postgres session variable), and database policy (RLS). Any one layer failing should not leak data.
2. **Zero behaviour change for working code.** Every existing test, every working REST endpoint, every NATS handler must continue to work unchanged once RLS is enabled.
3. **Explicit cross-tenant operations.** Maintenance loops, scheduler, and a few resolver hatches legitimately span tenants. They must be **physically separated** from business code, not just labelled by convention.
4. **Per-table, per-PR rollout.** Each table can have RLS enabled or disabled with a single statement. A failure on one table does not require rolling back the others.
5. **No bypass via role privilege.** `FORCE ROW LEVEL SECURITY` on every protected table ensures that even the table owner is subject to policies. The only escape is an explicitly distinct `BYPASSRLS` role used by maintenance paths.
6. **Static-checked discipline.** Convention is fragile. The few inviolable rules — "webui never imports admin primitives", "all `resolve_*` functions have retirement metadata" — are enforced by CI tests that parse the source, not by docstring warnings.

---

## Alternatives Considered

### Option A — Application-Layer Filtering Only (the pre-RLS state)

Stay with `WHERE tenant_id = $N` everywhere; don't introduce RLS at all.

**Pros**
- No new infrastructure (roles, policies, GUCs).
- Simpler test setup — one Postgres role, no policy interaction.
- Easier to debug — `EXPLAIN` plans are unmodified.

**Cons**
- One missed `WHERE` clause is one cross-tenant leak.
- SQL injection bypasses the entire model.
- Operators with `psql` access have no constraint.
- No defense against future "I'll just run a quick ad-hoc query" routes.

**Rejected.** Defense-in-depth with database enforcement is industry standard for shared-schema multi-tenancy at this risk level. The cost is bounded.

### Option B — RLS Only, No Application-Layer Filter

Remove the `WHERE tenant_id` clauses from application SQL and let RLS be the single guard.

**Pros**
- Less code.
- Single source of truth for tenant boundary.
- No risk of the two layers disagreeing.

**Cons**
- No defense if RLS is misconfigured on a single table or if GUC isn't set on a connection.
- Composite indexes `(tenant_id, id)` become useful only via RLS — query planner may not exploit them as effectively.
- Worse failure mode: a connection with no GUC set returns zero rows silently, looking like "user has no data" rather than a clear error.
- Application code becomes opaque about its tenant intent ("why is this query reading `safety_decisions` without any tenant context in sight?").

**Rejected.** The application-layer filter is cheap and explicit. Keeping it as defense-in-depth catches the "forgot to set GUC" class of bug at the query layer rather than in production.

### Option C — Session Abstraction Replacing `tenant_id` Parameter

Introduce `TenantSession` and `AdminSession` as typed objects threaded as the first argument to every DB function. The session carries both the connection and the implicit tenant context; functions never see a raw `tenant_id` string.

**Pros**
- Stronger type discipline (`TenantSession` and `AdminSession` are distinct types, no smuggling).
- Cross-tenant intent is visible in function signatures, not just docstrings.
- Cleaner per-function call sites (the `tenant_id` kwarg disappears).

**Cons**
- All ~60 DB functions and all callers must be refactored in one sweep.
- All test fixtures must be rewritten to provide sessions.
- Python's type system is too weak to fully prevent type smuggling; the discipline rests on mypy + lint.
- Connection lifetime tied to session lifetime introduces new failure modes (long-held sessions, leaks).
- The marginal safety improvement over the current pattern (compile-time error on missing `tenant_id` kwarg) is real but smaller than the refactor cost.

**Rejected.** Considered seriously; the refactor cost was too high for a codebase that already has a working `tenant_id` kwarg pattern. The current design preserves the option to migrate to sessions later if the team finds the kwarg noise intolerable.

### Option D — Schema-Per-Tenant or Database-Per-Tenant

Physical isolation: each tenant gets its own Postgres schema (or database), and the application sets `search_path` per request.

**Pros**
- Hardest possible isolation — no JOIN bug can cross tenants.
- Per-tenant backup and restore is trivial.
- No `tenant_id` columns, no RLS, no GUCs.

**Cons**
- DDL has to be applied to N schemas, multiplying migration complexity.
- Cross-tenant analytics (billing, reporting) becomes a separate problem.
- Connection pool overhead grows linearly with tenant count.
- Architectural change of this magnitude is incompatible with the existing data layer.

**Rejected.** Suitable for high-compliance scenarios (financial, healthcare) or very low tenant counts. For RoleMesh's target — many small-to-medium tenants on shared infrastructure — RLS in a shared schema is the standard fit.

---

## Architecture

### Three-Layer Defense

```
Layer 1: Application Parameter
    Function signature: get_X(id, *, tenant_id)  # required kwarg
    SQL filter:         WHERE id = $1 AND tenant_id = $2
    Catches:            forgotten checks at call site, indexes (tenant_id, id)

Layer 2: Connection Context
    On connection acquire (in transaction):
        SELECT set_config('app.current_tenant_id', $1, true)
    Catches:            connection used without context (fail-closed)

Layer 3: Database Policy
    Per table:          ENABLE ROW LEVEL SECURITY + FORCE
                        POLICY USING (tenant_id = current_tenant_id())
    Catches:            SQL injection, raw queries, role bypass attempts
```

Layer 1 is the legacy that the `tenant_id` kwarg refactor established. Layer 2 and Layer 3 are added by the RLS work.

The three layers are **redundant by design**. After RLS is enabled, Layer 1's `WHERE tenant_id = $2` returns the same rows as Layer 3 would have allowed. That redundancy is the whole point — it means a misconfigured policy on one table doesn't immediately leak through, because the application is still asking for the right tenant.

### Dual Pool, Dual Role

```
┌─────────────────────────────────────────────────────────────┐
│   App Pool   (role: rolemesh_app,  NOBYPASSRLS)                 │
│   ├── Used by: webui REST handlers, NATS business handlers  │
│   └── Wrapped by: tenant_conn(tenant_id) context manager    │
├─────────────────────────────────────────────────────────────┤
│   Admin Pool (role: rolemesh_system, BYPASSRLS)                   │
│   ├── Used by: maintenance loops, schedulers, resolvers,    │
│   │           startup migrations                            │
│   └── Wrapped by: admin_conn() context manager              │
└─────────────────────────────────────────────────────────────┘
```

**Why two pools instead of `SET ROLE` switching.** A single pool with role switching is fragile: forgetting to `RESET ROLE` before returning a connection to the pool means the next request gets the wrong privilege. Physical separation makes that mistake impossible — business code never has access to the admin pool object.

**Why two roles instead of two users with the same privileges.** Role privileges are checked by Postgres on every query. `BYPASSRLS` is a per-role attribute; making it a pool-property would require application logic, which is exactly the smuggling risk we're avoiding.

### Four-Function Class Taxonomy

Every database function falls into exactly one of four classes:

| Class | Connection | Signature | Returns | Examples |
|---|---|---|---|---|
| **A. Tenant-scoped business** | `tenant_conn(tenant_id)` | `tenant_id` required kwarg | Full rows | `get_coworker`, `list_safety_rules` |
| **B. Cross-tenant maintenance** | `admin_conn()` | No `tenant_id` param | Rows with `tenant_id` for downstream dispatch | `list_due_scheduled_tasks`, `cleanup_old_safety_decisions` |
| **C. Tenant resolver (boundary bootstrap)** | `admin_conn()` | No `tenant_id` (output is authoritative) | **Minimal scalar** (str / tuple) | `resolve_coworker_tenant`, `resolve_user_for_auth` |
| **D. Startup / DDL** | `admin_conn()` | Free-form | Free-form | `init_database`, `_create_schema` |

The class is not just documentation — it is structurally enforced:
- A-class functions cannot reach the admin pool (it is not exported to `webui/`).
- C-class functions are name-prefixed `resolve_*` and CI tests verify they only return minimal scalars and only appear in whitelisted callers.
- B and D classes are isolated by virtue of the call sites being known (engine reconcile loops, scheduler, app startup).

The reason for treating C separately from B, despite both running on `admin_conn`, is **trust scope**. B-class functions return rows containing a `tenant_id` field, which downstream code uses to re-scope into `tenant_conn(row.tenant_id)`. C-class functions return *only* a `tenant_id` (or `(tenant_id, role)`), and their callers must immediately use it to construct a tenant-scoped session. Returning a full row from C would defeat the purpose: if the caller could trust the row from an admin connection, they could skip the tenant-scoped re-read, and RLS would have been bypassed.

### Tenant Resolver Contract

Tenant resolvers exist because some entry points genuinely don't have tenant context yet:

- **NATS legacy fallback.** A subject like `agent.<job_id>.results` may arrive without `tenant_id` in the body (e.g., a message published before the protocol carried tenant explicitly). The executor must resolve the job's tenant before any RLS-scoped work can happen.
- **JWT resume.** When a user presents a signed JWT carrying only `user_id`, the auth provider must look up that user's tenant before any session can be constructed.

These are the **only** legitimate uses. Every resolver carries metadata documenting:
- **Type**: structural (permanent, like JWT resume) or legacy (removable when some condition is met).
- **Allowed callers**: explicit file paths. CI checks no other module imports the resolver.
- **Removal tracking**: for legacy resolvers, the condition under which they can be deleted.

This metadata is not optional. The CI suite parses the source and rejects any `resolve_*` function missing the metadata block.

### Schema Co-Design

Two tables deserve explicit mention because they're shaped by RLS:

- **`safety_rules_audit`** has a denormalized `tenant_id` column (copied from `safety_rules` via insert trigger) and a composite foreign key `(rule_id, tenant_id) → safety_rules(id, tenant_id)`. The trigger keeps writes ergonomic; the composite FK makes drift impossible at the database level even if the trigger is disabled. The hot read path uses a composite index `(tenant_id, rule_id, created_at)` — an index seek on the first two columns followed by a sorted scan.
- **`oidc_user_tokens`** is structurally a user-level table (user_id is the natural key), but a denormalized `tenant_id` column is added and synchronized from `users.tenant_id` via the same trigger + composite FK pattern. Without this, the table cannot have a sensible RLS policy.

Two tables explicitly do not have RLS:
- **`tenants`** is the root table; access is via owner endpoints and admin connection only.
- **`external_tenant_map`** is the OIDC tenant lookup table; `rolemesh_app` has no privileges on it.

---

## Key Tradeoffs

### Silent Mismatch vs. Information Disclosure

When application code calls `get_safety_rule(rule_id, tenant_id="wrong")`, the function returns `None`. This is indistinguishable from "the rule does not exist." The behavior is intentional — distinguishing the two would let an attacker probe for the existence of resources in other tenants.

The downside is debugging. A new endpoint with a bug that passes the wrong `tenant_id` produces "not found" symptoms, sending developers to look in the wrong place. The mitigation is that the most security-sensitive by-id functions (`get_safety_rule`, `get_user`, `list_safety_rule_audit`) use a CTE pattern that detects the mismatch internally and emits a structured warning log with metric `tenant_mismatch_attempted` — observable to operators without being exposed to callers.

### Verbosity vs. Auditability

Every business call site carries `tenant_id=user.tenant_id` as an explicit keyword argument. This is noisy compared to implicit context (contextvars) or session abstractions. The trade is that any cross-tenant intent is visible at the call site: a `grep` for `admin_conn` enumerates every code path that legitimately crosses tenants. The verbosity was accepted as the cost of that auditability.

### Trigger Convenience vs. Drift Risk

`safety_rules_audit.tenant_id` is populated by an insert trigger, not by application code. Triggers can be silently disabled by a DBA, and a new application path that bypasses the trigger writes nothing to the column. The composite foreign key `(rule_id, tenant_id) → safety_rules(id, tenant_id)` defends against both: a row with a NULL or wrong `tenant_id` cannot be inserted at all. The trigger remains for ergonomics; the FK is the safety net.

### Two Pools vs. One Pool

A single pool with `SET ROLE rolemesh_app` / `SET ROLE rolemesh_system` would save memory and connection slots. The risk — forgetting to reset role and leaking admin privilege to the next request — was judged unacceptable for this risk profile. The extra pool costs a few connections; it is a cheap form of physical isolation.

---

## Migration Path

RLS was rolled out in five sequential phases. The phases are designed so that each one can be deployed and rolled back independently, and so that no phase weakens the system relative to the previous one.

1. **Application-layer pinning.** Add `tenant_id` required kwarg to remaining by-id functions; add `resolve_user_for_auth` for JWT resume; rewrite auth providers to use the two-step bootstrap pattern. No DB changes yet.
2. **Infrastructure.** Create the `current_tenant_id()` SQL function, the `rolemesh_app` / `rolemesh_system` roles, the dual pool, and the `tenant_conn` / `admin_conn` wrappers. RLS is still off; no business behavior changes.
3. **Connection migration.** Replace every `pool.acquire()` site with `tenant_conn(tenant_id)` or `admin_conn()` based on the function's class. After this phase, every business path correctly carries tenant context — but RLS is still not enforced.
4. **Per-table enablement.** Enable RLS one table at a time, starting with the lowest-blast-radius table (`safety_rules_audit`) as a canary and ending with `users` and `oidc_user_tokens`. Each table is a single commit, independently rollback-able with one `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` statement.
5. **Enforcement tests.** Add tests that verify RLS actually blocks cross-tenant access at the DB level (distinct from application-layer tests that verify the WHERE clause). Add static-analysis CI checks that the four-class taxonomy is preserved.

The order is critical: phase 4 cannot precede phase 3, because turning on RLS while a path still uses raw `pool.acquire()` would silently break that path (no GUC set → fail-closed → empty results).

---

## What This Architecture Does NOT Do

- **It does not protect against compromised `rolemesh_system` credentials.** Anyone holding the admin role has full cross-tenant access. That role's credentials must be treated with the same care as the Postgres superuser.
- **It does not partition the NATS message bus per tenant.** A compromised container could subscribe to subjects like `agent.*.tasks` and observe (but not modify) other tenants' messages. The engine validates tenant on write paths, but read-side observation is documented as an XFAIL in `tests/attack_sim/test_E_tenant_isolation.py` (`test_E6`).
- **It does not provide per-tenant resource quotas.** Concurrency limits exist (`max_concurrent_containers`), but token/spend/API quotas are out of scope.
- **It does not retire `resolve_*` resolvers automatically.** Each resolver carries removal metadata, but the actual removal is a manual decision by a future PR when the documented condition is met.
- **It does not enforce role discipline at runtime.** The "webui never imports `admin_conn`" rule is checked at CI time by an AST test. A determined developer working around it would not be blocked by runtime mechanisms.

---

## Operational Considerations

### Connecting with `psql`

Operator `psql` sessions default to the Postgres superuser, which has `BYPASSRLS` and sees everything. To simulate the application's view:

```sql
SET ROLE rolemesh_app;
SELECT set_config('app.current_tenant_id', '<tenant_uuid>', false);
-- subsequent queries are RLS-scoped to that tenant
```

`false` instead of `true` makes the setting persistent for the session, useful for ad-hoc inspection.

### Backup and Restore

`pg_dump` includes RLS policies and `FORCE` settings. `pg_restore` must run as superuser (to create roles and policies). After restore, the application's `_create_schema` is idempotent and reconciles any drift.

### Diagnosing "User Sees Empty Results"

If a user reports that data they should see is missing, check in order:

1. Is the user's JWT being decoded to the correct `tenant_id`? (`auth/oidc/provider.py` logs include the resolved tenant.)
2. Is the connection going through `tenant_conn`? Add a temporary log of `current_setting('app.current_tenant_id')` at the suspected query.
3. Is the RLS policy on the relevant table what you expect? `SELECT * FROM pg_policies WHERE tablename = '<table>'`.
4. Is the role `rolemesh_app` or did something fall through to `rolemesh_system`? `SELECT current_user` in the connection.

### Adding a New Tenant-Scoped Table

When a new tenant table is added:

1. Add `tenant_id UUID NOT NULL REFERENCES tenants(id)` (consider `ON DELETE CASCADE`).
2. Add a composite index `(tenant_id, <hot lookup column>)` if reads are frequent.
3. Add the standard four RLS policies (SELECT / INSERT / UPDATE / DELETE) plus `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY` in `_create_schema`.
4. Add CRUD functions following the A-class pattern (`tenant_id` required kwarg, `tenant_conn(tenant_id)` wrapper).
5. Add a cross-tenant isolation test in `tests/db/test_cross_tenant_isolation.py`.

The CI AST tests will catch most violations of the pattern; reading them is recommended before adding new functions.

---

## Related Documentation

- [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) — tenant data model, entity hierarchy, message routing
- [`6-auth-architecture.md`](6-auth-architecture.md) — `AgentPermissions`, JWT resume flow that uses `resolve_user_for_auth`
- [`15-safety-framework-architecture.md`](15-safety-framework-architecture.md) — safety rule audit schema and the trigger that synchronizes `tenant_id`
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) — NATS subjects and the legacy fallback that `resolve_coworker_tenant` exists to serve
