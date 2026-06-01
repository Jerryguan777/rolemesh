# RoleMesh test suite — conventions

This is the standard for how tests in this repo are written, organized, and
run. New tests follow it; existing tests are migrated toward it. The goal is
a suite that **finds real bugs**, not one that maximizes coverage or proves
the current implementation re-states itself.

## Philosophy: tests exist to catch bugs

A suite with high coverage that has never caught a bug is a failed suite.
Four red lines — a test that crosses one should be rewritten, not merged:

1. **No mirror tests.** Do not read an implementation and then write a test
   that walks the same branches and compares outputs. If your test is the
   function re-implemented, it catches nothing. Test against the *spec /
   intended behavior*, ideally written down before reading the code.
2. **Mock only at true external boundaries.** Network, the Docker daemon,
   a real clock, a third-party HTTP API — fine. Never mock internal modules
   or helpers; every internal mock is a place a real bug hides. Prefer real
   collaborators. The DB is *not* a boundary to mock here — we run a real
   Postgres (see below).
3. **Cover edges and adversaries, not just the happy path.** Empty / null /
   single-element inputs, boundaries and off-by-one, concurrency and
   ordering, idempotency (call it twice), injection, malformed-but-valid
   shapes. For isolation/security code the *forbidden* path (cross-tenant
   read, escape attempt) is the whole point — assert it is blocked.
4. **No tautological asserts.** `assert isinstance(X, str)` on a constant, or
   `assert mock.return_value == mock.return_value`, can never fail. Assert an
   independent, meaningful property of real output.

When you fix a bug, first add a test that fails before the fix and passes
after, with a comment noting what it caught.

## Test tiers and markers

Tests are layered by what they require to run. The tier is expressed with a
pytest marker; the default run excludes the two slow/external tiers.

| Tier | Marker | Requires | Runs by default? |
|------|--------|----------|------------------|
| **Unit** | *(none)* | pure Python, in-process | yes |
| **DB-integration** | *(none)* | a real Postgres (auto via testcontainers) | yes |
| **Integration** | `integration` | a live Docker daemon | no — nightly / opt-in |
| **E2E** | `e2e` | real Anthropic API + Docker + NATS | no — manual / nightly |

The default selection lives in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not integration and not e2e'"
```

DB-integration tests are *unmarked* on purpose: a Postgres container is cheap
to spin up and we want tenant-isolation/RLS regressions caught on every PR.
Reserve the `integration` marker for tests that need the Docker daemon (pull
images, launch containers) and `e2e` for tests that hit a real model or the
full NATS-backed orchestrator.

### Running

```bash
uv run pytest                      # default: unit + DB-integration (what CI runs)
uv run pytest tests/db             # one area
uv run pytest -m integration       # Docker-daemon tests
uv run pytest -m e2e               # real-model / full-stack e2e
uv run pytest -m ''                # everything
```

### How a test gets its marker

- **`integration`**: the directory's `conftest.py` sets a module-level
  `pytestmark = pytest.mark.integration` (see
  `tests/container/integration/conftest.py`). Any test placed there inherits
  it.
- **`e2e`**: an `e2e/` directory's `conftest.py` stamps the marker in a
  `pytest_collection_modifyitems` hook **filtered by path**.

> ⚠️ **`pytest_collection_modifyitems` is session-wide.** Its `items`
> argument is *every* collected test in the run, not only those under the
> conftest's directory. A hook that marks all items unconditionally will
> stamp the **entire suite** — which once made `-m 'not ... and not e2e'`
> deselect everything and CI pass while running zero tests. Always filter:
>
> ```python
> _THIS_DIR = Path(__file__).parent
> def pytest_collection_modifyitems(config, items):
>     for item in items:
>         if _THIS_DIR in item.path.parents:
>             item.add_marker(pytest.mark.e2e)
> ```
>
> `tests/test_marker_discipline.py` guards this — it fails if default
> collection collapses or a marker leaks suite-wide. Do not delete it.

## Real dependencies, not mocks

- **Postgres**: `tests/conftest.py` provides a session-scoped `pg_url`
  (testcontainers `postgres:16`) and a per-test `test_db` fixture that
  recreates a fresh schema. Use these; do not mock the DB layer. RLS and
  cross-tenant isolation must be exercised against the real engine.
- **Docker / NATS / model APIs**: mock or stub only at the wire boundary,
  and put the test behind the `integration` or `e2e` marker.

## Layout

```
tests/
  <area>/                 # unit + DB-integration for one subsystem
    test_*.py
    e2e/                  # full-stack e2e for that area (e2e marker via conftest)
    integration/          # Docker-daemon tests (integration marker via conftest)
  test_*.py               # cross-cutting integration that spans several areas
  conftest.py             # shared fixtures (pg_url, test_db, tmp dirs)
  test_marker_discipline.py   # CI-integrity guard — keep
```

Name tests by the behavior they pin, not the function they call:
`test_cross_tenant_read_is_blocked`, not `test_get_user`. One failure mode
per test so a red test names the bug.

## Exemplar files — copy these patterns

| Pattern | Reference |
|---------|-----------|
| RLS / cross-tenant isolation against real Postgres | `tests/db/test_rls_enforcement.py`, `tests/db/test_cross_tenant_isolation.py` |
| Invariant / property-style sweep (no hypothesis dep) | `tests/container/test_hardening_invariants.py` |
| FastAPI surface + real DB, validation + isolation | `tests/webui/test_skills_api.py` |
| Wire-format round-trip (serialize → DB → read back) | `tests/test_e2e_usage_pipeline.py` |
| Boundary / off-by-one / mutation-minded asserts | `tests/evaluation/test_scorers_tool_trace.py` |
| Fail-close hook dispatch with explicit mutation notes | `tests/test_agent_runner/test_hook_registry.py` |
| Adversarial / attack-surface tests | `tests/attack_sim/`, `tests/safety/` |

## Checklist before opening a PR with new tests

- [ ] Tier is correct and the marker is applied (Docker → `integration`,
      real model / full stack → `e2e`).
- [ ] No internal module is mocked; only true boundaries are.
- [ ] At least one edge/adversarial case, not only the happy path.
- [ ] The test fails if you mentally mutate the code under test
      (`<` → `<=`, `+1` → `-1`, `and` → `or`). If nothing fails, the test
      has a hole.
- [ ] Bug-fix tests include a comment on what they caught.
- [ ] `uv run pytest` (default tier) still collects and passes locally.
