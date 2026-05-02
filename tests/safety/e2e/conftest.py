"""E2E fixtures for safety tests.

Auto-applies the ``e2e`` marker to every test in this directory so the
default ``addopts = "-m 'not integration and not e2e'"`` in
``pyproject.toml`` actually keeps these out of fast PR runs. Without
this hook, the ``e2e`` directory name is just convention — pytest sees
plain unmarked tests and runs them, which requires the safety
framework's RPC server / DB / orchestrator side state on every PR
worker. Operators who want the e2e tests opt back in with
``pytest -m e2e`` or ``pytest -m ""`` (include everything).
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Stamp every test under this directory with the ``e2e`` marker.

    See ``tests/approval/e2e/conftest.py`` for the rationale.
    """
    for item in items:
        item.add_marker(pytest.mark.e2e)
