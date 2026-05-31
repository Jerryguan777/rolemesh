# Contracts

This folder is the source of truth for the API surface between the
RoleMesh frontend (`web/`) and backend (`src/webui/`, `src/rolemesh/`).
Neither side owns it — both sides derive from it.

## Files

- **`openapi.yaml`** — REST API contract (OpenAPI 3.1). Covers every
  `/api/v1/*` endpoint and the JSON schemas they exchange.

## Derived artifacts (do not edit by hand)

- **`web/src/api/generated/types.ts`** — TypeScript types generated
  from `openapi.yaml` via `openapi-typescript`. Regenerate with
  `npm run openapi:gen` from `web/`. Committed so first-checkout
  works without bootstrapping the web tree.
- **`src/webui/schemas_v1.py`** — Hand-written Pydantic models that
  must stay in sync with the yaml. There's no generator; we maintain
  them by hand and a drift test catches divergence.

## Drift protection

Two CI tests fail loudly when the contract gets out of sync:

| Test | Catches |
|---|---|
| `tests/test_openapi_codegen_freshness.py` | yaml changed but `types.ts` not regenerated |
| `tests/test_openapi_contract.py` | yaml field shape differs from the Pydantic model claiming the same `$ref` |

Both run on every PR. A contract change is incomplete until both pass.

## Workflow for changing the contract

1. Edit `contracts/openapi.yaml`.
2. From `web/`: `npm run openapi:gen` — regenerates `types.ts`. Commit
   the result.
3. Update the matching Pydantic model in `src/webui/schemas_v1.py`.
4. Run `pytest tests/test_openapi_contract.py
   tests/test_openapi_codegen_freshness.py` — both must pass.
5. Update tests / handlers / frontend call sites in the same PR.

A contract change that lands in a separate PR from its implementation
will silently break one side until the other catches up. Keep them
together.

## Policy decisions

Long-running design rules that aren't enforceable by a test. Each
entry exists because following the rule's spirit, not just its
letter, requires understanding the rationale — and the rationale
won't fit in a test name.

### Auth namespace: per-protocol, NOT unified

**Decision** (2026-05-24): endpoints under `/api/v1/auth/*` are
reserved for auth-mode-agnostic operations (the SPA calls them the
same way regardless of how the user logged in). Mode-specific
protocol mechanics live in their own top-level namespaces:

| Namespace | Contains |
|---|---|
| `/api/v1/auth/*` | `config`, `ws-ticket`, future `logout`. Mode-agnostic. |
| `/oidc/*` | OIDC protocol mechanics: `login`, `callback`, `refresh`. |
| `/builtin/*` (future) | builtin username/password: `login`, `refresh`. |
| `/api/v1/me` | Identity surface. Mode-agnostic. |

**Why not unify everything under `/api/v1/auth/*`**:

1. OIDC and builtin have **structurally different protocol shapes**
   (browser redirect vs. POST→JSON). Forcing them under a single
   `/auth/login` URL means an OpenAPI `oneOf` body schema — the
   frontend has to branch on mode to construct the body anyway, so
   no real consolidation.
2. OIDC's `/oidc/callback` URL is **registered at the IdP** (Google,
   Okta, Auth0). Moving it would force every production deployment
   to update IdP configuration. The cost of breaking IdP configs
   outweighs the aesthetic benefit of namespace uniformity.
3. The principle is: **URL shape should follow protocol identity,
   not be forced into a single namespace for aesthetic uniformity.**

**Exception — `/api/v1/auth/logout` SHOULD be unified** (TODO,
lands with builtin). The frontend doesn't care how the session was
created, only that it gets killed; the handler dispatches internally
to the matching protocol's revoke flow. This is the one `/auth/*`
endpoint where mode-agnostic consolidation beats per-protocol
separation, because the call site cost (frontend branching on mode
just to log out) is real and the implementation cost (a small
internal dispatch) is trivial.

**Compared to Tropos**: Tropos's contract unifies all 5 auth
operations under `/auth/*`. Their design works because they appear
to have one auth mode; we have at least three (bootstrap / OIDC /
future builtin), and the cost-benefit tilts the other way.

A signpost comment lives at the top of the `paths:` block in
`openapi.yaml` next to the `/api/v1/auth/*` endpoints — devs
editing that section won't miss it.
