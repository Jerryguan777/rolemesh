"""E2E fixtures for approval tests.

The ``test_db`` fixture comes from the repo-level ``tests/conftest.py``
(recreates schema per test). ``nats_url`` here assumes the dev NATS
server is reachable; the test suite as a whole will skip this module
if NATS is not available.

Auto-applies the ``e2e`` marker to every test in this directory so the
default ``addopts = "-m 'not integration and not e2e'"`` in
``pyproject.toml`` actually keeps these out of fast PR runs. Without
this hook, the ``e2e`` directory name is just convention — pytest sees
plain unmarked tests and runs them, which (a) requires NATS / Docker
on every CI worker and (b) sequentially exercises overlapping
JetStream consumers across the harness's per-test ephemeral runs.
Operators who want the e2e tests opt back in with ``pytest -m e2e``
or ``pytest -m ""`` (include everything).
"""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .harness import OrchestratorHarness

_NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")


def _nats_reachable(url: str) -> bool:
    """Best-effort probe of NATS availability.

    We skip the entire E2E module if NATS is unreachable — CI without
    the dev stack should not see false failures here. Local runs with
    docker-compose.dev.yml up will pick NATS up automatically.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4222
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


if not _nats_reachable(_NATS_URL):
    pytest.skip(
        f"NATS not reachable at {_NATS_URL}; skip E2E. "
        "Start with: docker compose -f docker-compose.dev.yml up -d",
        allow_module_level=True,
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Stamp every test under this directory with the ``e2e`` marker.

    Pytest auto-loads the nearest conftest, so this hook only fires
    for items collected under ``tests/approval/e2e/``. Done at the
    conftest level rather than by adding ``pytestmark`` to every file
    so a new test file in this dir gets the marker by default and
    can't accidentally leak into a fast PR run.
    """
    for item in items:
        item.add_marker(pytest.mark.e2e)


@pytest.fixture
def nats_url() -> str:
    return _NATS_URL


@pytest.fixture
async def harness(
    test_db: None, nats_url: str
) -> AsyncIterator[OrchestratorHarness]:
    """Per-test orchestrator boot. Uses a fresh PG schema + isolated
    NATS durable consumers so tests cannot leak state to each other."""
    from .harness import orchestrator_harness

    async with orchestrator_harness(nats_url) as h:
        yield h
