"""E2E fixtures for approval tests.

The ``test_db`` fixture comes from the repo-level ``tests/conftest.py``
(recreates schema per test). ``nats_url`` here assumes the dev NATS
server is reachable; the test suite as a whole will skip this module
if NATS is not available.
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
