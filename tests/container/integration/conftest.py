"""Shared fixtures for hardening E2E tests.

Marked `integration` at the module level so the whole file is skipped
unless the caller passes `-m integration`. Expects a reachable Docker
daemon; skip gracefully if the daemon probe fails so running the suite
on a CI runner without Docker is a no-op rather than a failure cascade.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import aiodocker
import aiodocker.exceptions
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration

PROBE_IMAGE = "alpine:3.19"


@pytest.fixture
async def docker_client() -> AsyncIterator[aiodocker.Docker]:
    """Per-test aiodocker client.

    Must be function-scoped: aiodocker ties its aiohttp connector to the
    event loop at construction time, and pytest-asyncio creates a fresh
    loop per test. A session-scoped client crashes with "attached to a
    different loop" on the second test.

    Each client open/close adds ~30ms per test — acceptable trade for
    the 5 tests in this module. The probe image is pulled once in a
    module-level fixture below so we don't pay the pull cost per test.
    """
    client = aiodocker.Docker()
    try:
        await client.system.info()
    except (OSError, aiodocker.exceptions.DockerError) as exc:
        await client.close()
        pytest.skip(f"Docker daemon unreachable: {exc}")
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _pull_probe_image() -> None:
    """Pull alpine:3.19 once per module run using the docker CLI (sync).

    Using the CLI sidesteps the event-loop-scope problem of a session
    aiodocker client and runs before any per-test loop is even created.
    """
    import subprocess
    r = subprocess.run(
        ["docker", "image", "inspect", PROBE_IMAGE],
        capture_output=True, check=False,
    )
    if r.returncode == 0:
        return
    r = subprocess.run(
        ["docker", "pull", PROBE_IMAGE],
        capture_output=True, check=False,
    )
    if r.returncode != 0:
        pytest.skip(f"could not pull {PROBE_IMAGE}: {r.stderr.decode()[:200]}")


@pytest.fixture
def unique_name() -> str:
    """Per-test uuid suffix so parallel runs / crashed prior runs don't collide."""
    return f"rmtest-{uuid.uuid4().hex[:8]}"
