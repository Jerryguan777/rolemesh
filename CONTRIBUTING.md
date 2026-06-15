# Contributing to RoleMesh

Thanks for your interest in contributing! This guide covers how to set up a
development environment, the checks your change must pass, and the pull-request
flow.

## Before you start

- For anything beyond a small fix, please open an issue first so we can agree on
  the approach before you spend time on it.
- By contributing, you agree that your contributions are licensed under the
  project's [AGPL-3.0-or-later](LICENSE) license.
- Found a security issue? **Do not open a public issue** — follow
  [`SECURITY.md`](SECURITY.md) instead.

## Development setup

Prerequisites: Docker, Python 3.12+, and [uv](https://github.com/astral-sh/uv).

```bash
# Clone your fork
git clone https://github.com/<your-username>/rolemesh.git
cd rolemesh

# Install dev + pi + eval extras
uv sync --extra pi --extra dev --extra eval
```

See the [Quick Start](README.md#quick-start) in the README for running the full
stack with Docker Compose.

## Checks your change must pass

Run these locally before pushing — CI runs the same checks:

```bash
# Unit and integration tests (Postgres via testcontainers)
uv run pytest

# Type-check and lint
uv run mypy src
uv run ruff check src tests

# Verify container hardening (when touching the sandbox/runtime)
scripts/verify-hardening.sh
```

## Pull-request flow

1. Create a topic branch off `main`. The CI workflow triggers automatically on
   pull requests, and on direct pushes to the `feat/**`, `fix/**`, `safety/**`,
   and `ci/**` branch namespaces.
2. Keep commits focused and write clear commit messages explaining the *why*.
3. Add or update tests for any behavior change.
4. Update the relevant docs under `docs/` (and the `-cn` Chinese counterpart if
   you are comfortable doing so).
5. Open a PR with a description of the change and the motivation. Make sure CI
   is green.

## Code style

- Follow the existing patterns in the surrounding code.
- Linting and formatting are enforced by [Ruff](https://docs.astral.sh/ruff/)
  (`ruff.toml`); type-checking by [mypy](https://mypy-lang.org/).
- Prefer small, reviewable changes over large omnibus PRs.

## Documentation

Architecture docs live under `docs/`, numbered by topic. Most have a Chinese
counterpart with a `-cn` suffix. If your change affects documented behavior,
update the corresponding doc in the same PR.
