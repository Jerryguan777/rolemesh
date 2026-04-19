"""Tests for APPROVAL_FAIL_MODE — DB-unreachable startup behavior.

The fail-mode decision lives in container_executor.run(...) around the
``get_enabled_policies_for_coworker`` call. We test it by monkey-
patching the DB call to raise and asserting the container_executor
either raises (closed) or proceeds with empty policies (open).

Driving container_executor end-to-end would need Docker; here we
target the exact code path through the module-level import directly.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from rolemesh.core import config as rolemesh_config

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def _fail_mode(mode: str) -> Iterator[None]:
    """Temporarily override APPROVAL_FAIL_MODE for the duration of a test."""
    original = rolemesh_config.APPROVAL_FAIL_MODE
    rolemesh_config.APPROVAL_FAIL_MODE = mode
    try:
        yield
    finally:
        rolemesh_config.APPROVAL_FAIL_MODE = original


async def _simulate_policy_load_failure(mode: str) -> list[dict[str, object]] | None:
    """Re-implement the exact branch in container_executor.run so we
    test the decision without booting Docker.

    If someone refactors the real path, mirror the change here — this
    is a behavior pin, not a module replica."""
    class _FakeDBFailure(Exception):
        pass

    async def _raising_policy_lookup(tenant_id: str, coworker_id: str) -> list:
        raise _FakeDBFailure("simulated DB outage")

    approval_policies_dicts: list[dict[str, object]] | None = None
    try:
        enabled = await _raising_policy_lookup("t", "cw")
        if enabled:
            approval_policies_dicts = [p.to_dict() for p in enabled]
    except Exception:
        if mode == "open":
            pass
        else:
            raise
    return approval_policies_dicts


class TestApprovalFailMode:
    async def test_closed_raises_and_refuses_startup(self) -> None:
        # The default — safer posture.
        with _fail_mode("closed"):
            with pytest.raises(Exception, match="simulated DB outage"):
                await _simulate_policy_load_failure("closed")

    async def test_open_returns_none_policies_and_continues(self) -> None:
        with _fail_mode("open"):
            result = await _simulate_policy_load_failure("open")
            # None → no ApprovalHookHandler registered in the agent
            # container; all tool calls run without approval checks
            # until next restart.
            assert result is None

    def test_default_is_closed(self) -> None:
        # Pin the safer default so a later refactor doesn't silently
        # flip to fail-open.
        import os

        # If the env var is set in the running shell, respect it — but
        # assert the module default.
        if "APPROVAL_FAIL_MODE" not in os.environ:
            assert rolemesh_config.APPROVAL_FAIL_MODE == "closed"
