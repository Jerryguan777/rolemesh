"""Startup-sequence invariants for _ensure_container_system_running().

The function's body is imperative — there is no type signature forcing
the three steps to happen in the right order, and a future refactor can
silently reorder them. These tests pin the invariants:

  1. ensure_available() failure must short-circuit the rest. If it raises
     IncompatibleDockerVersionError or a connection error we must NOT
     create an agent network (a half-initialised network is harder to
     clean up than no network), and we must NOT call cleanup_orphans
     (which would fail against a broken client).

  2. ensure_agent_network() failure must prevent cleanup_orphans. Leaving
     orphan-cleanup running against a partially-initialised state makes
     debugging harder — bail at the first error.

  3. Happy path: all three steps called, in order.

We mock the runtime entirely; the correctness of each individual method
is covered by test_docker_runtime.py / test_network.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_runtime_mock() -> MagicMock:
    rt = MagicMock()
    rt.ensure_available = AsyncMock()
    rt.ensure_agent_network = AsyncMock()
    rt.cleanup_orphans = AsyncMock(return_value=[])
    return rt


async def test_startup_happy_path_calls_all_three_in_order() -> None:
    from rolemesh import main as main_module

    # attach_mock lets parent.mock_calls track child calls in order across
    # multiple AsyncMocks — simpler than instrumenting each hook.
    parent = MagicMock()
    rt = _make_runtime_mock()
    parent.attach_mock(rt.ensure_available, "ensure_available")
    parent.attach_mock(rt.ensure_agent_network, "ensure_agent_network")
    parent.attach_mock(rt.cleanup_orphans, "cleanup_orphans")

    with patch.object(main_module, "get_runtime", return_value=rt):
        await main_module._ensure_container_system_running()

    # Order check: ensure_available BEFORE ensure_agent_network BEFORE cleanup_orphans
    call_order = [c[0] for c in parent.mock_calls]
    assert call_order == [
        "ensure_available",
        "ensure_agent_network",
        "cleanup_orphans",
    ]


async def test_startup_aborts_when_ensure_available_fails() -> None:
    """dockerd missing / wrong version → neither network nor cleanup runs."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()
    rt.ensure_available = AsyncMock(side_effect=RuntimeError("docker dead"))

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        pytest.raises(RuntimeError, match="docker dead"),
    ):
        await main_module._ensure_container_system_running()

    rt.ensure_agent_network.assert_not_awaited()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_aborts_when_network_creation_fails() -> None:
    """Network creation failure must short-circuit cleanup_orphans — leaving
    a half-initialised orchestrator is better than running cleanup against
    an unknown network state."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()
    rt.ensure_agent_network = AsyncMock(side_effect=RuntimeError("network failed"))

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        pytest.raises(RuntimeError, match="network failed"),
    ):
        await main_module._ensure_container_system_running()

    rt.ensure_available.assert_awaited_once()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_tolerates_runtime_without_network_hook() -> None:
    """Future k8s backend won't have ensure_agent_network. The current
    code uses hasattr() to skip it gracefully; pin that contract so a
    future 'require this method' change doesn't silently break non-Docker
    backends."""
    from rolemesh import main as main_module

    rt = MagicMock(spec=["ensure_available", "cleanup_orphans"])
    rt.ensure_available = AsyncMock()
    rt.cleanup_orphans = AsyncMock(return_value=[])

    with patch.object(main_module, "get_runtime", return_value=rt):
        await main_module._ensure_container_system_running()

    rt.ensure_available.assert_awaited_once()
    rt.cleanup_orphans.assert_awaited_once()
