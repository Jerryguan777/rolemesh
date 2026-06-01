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

from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Stamp tests under THIS directory with the ``e2e`` marker.

    ``pytest_collection_modifyitems`` is a session-wide hook: ``items``
    holds every collected test in the run, not just those under this
    conftest's directory. We MUST filter by path, or a full-suite
    collection from the repo root would mark the entire suite ``e2e``
    and the default ``-m 'not ... and not e2e'`` would deselect
    everything (vacuously green CI).
    """
    for item in items:
        if _THIS_DIR in item.path.parents:
            item.add_marker(pytest.mark.e2e)
