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
