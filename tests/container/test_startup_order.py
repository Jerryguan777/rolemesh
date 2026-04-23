"""Startup-sequence invariants for _ensure_container_system_running().

The function's body is imperative — there is no type signature forcing
the six steps to happen in the right order, and a future refactor can
silently reorder them. These tests pin the invariants:

  1. ensure_available() failure must short-circuit the rest.
  2. ensure_agent_network() failure must prevent every downstream step.
  3. ensure_egress_network() failure must prevent gateway launch + orphan
     cleanup.
  4. launch_egress_gateway() failure must prevent readiness probe + orphan
     cleanup.
  5. Happy path: all six steps called, in order.
  6. Backends without the EC-1 hooks (future k8s) fall back to the old
     surface gracefully — we use hasattr() gates for each hook so a
     minimal runtime doesn't have to stub them.

We mock the runtime entirely; correctness of each individual method is
covered by test_docker_runtime.py / test_network.py / test_egress_launcher.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_runtime_mock() -> MagicMock:
    """Mock with every EC-1 hook present.

    ``_ensure_client`` is included so the launch path can pull the
    aiodocker handle; we don't care what it returns because
    launch_egress_gateway is also mocked.
    """
    rt = MagicMock()
    rt.ensure_available = AsyncMock()
    rt.ensure_agent_network = AsyncMock()
    rt.ensure_egress_network = AsyncMock()
    rt.verify_egress_gateway_reachable = AsyncMock()
    rt.cleanup_orphans = AsyncMock(return_value=[])
    rt._ensure_client = MagicMock(return_value=MagicMock())
    return rt


async def test_startup_happy_path_calls_all_steps_in_order() -> None:
    from rolemesh import main as main_module

    parent = MagicMock()
    rt = _make_runtime_mock()
    parent.attach_mock(rt.ensure_available, "ensure_available")
    parent.attach_mock(rt.ensure_agent_network, "ensure_agent_network")
    parent.attach_mock(rt.ensure_egress_network, "ensure_egress_network")
    parent.attach_mock(rt.cleanup_orphans, "cleanup_orphans")

    launch_mock = AsyncMock()
    wait_mock = AsyncMock()

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        patch("rolemesh.egress.launcher.launch_egress_gateway", launch_mock),
        patch("rolemesh.egress.launcher.wait_for_gateway_ready", wait_mock),
    ):
        parent.attach_mock(launch_mock, "launch_egress_gateway")
        parent.attach_mock(wait_mock, "wait_for_gateway_ready")

        await main_module._ensure_container_system_running()

    call_order = [c[0] for c in parent.mock_calls if c[0]]
    assert call_order == [
        "ensure_available",
        "ensure_agent_network",
        "ensure_egress_network",
        "launch_egress_gateway",
        "wait_for_gateway_ready",
        "cleanup_orphans",
    ]


async def test_startup_aborts_when_ensure_available_fails() -> None:
    """dockerd missing / wrong version → every downstream step skipped."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()
    rt.ensure_available = AsyncMock(side_effect=RuntimeError("docker dead"))

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        pytest.raises(RuntimeError, match="docker dead"),
    ):
        await main_module._ensure_container_system_running()

    rt.ensure_agent_network.assert_not_awaited()
    rt.ensure_egress_network.assert_not_awaited()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_aborts_when_agent_network_fails() -> None:
    from rolemesh import main as main_module

    rt = _make_runtime_mock()
    rt.ensure_agent_network = AsyncMock(side_effect=RuntimeError("agent net failed"))
    launch_mock = AsyncMock()

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        patch("rolemesh.egress.launcher.launch_egress_gateway", launch_mock),
        pytest.raises(RuntimeError, match="agent net failed"),
    ):
        await main_module._ensure_container_system_running()

    rt.ensure_available.assert_awaited_once()
    rt.ensure_egress_network.assert_not_awaited()
    launch_mock.assert_not_awaited()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_aborts_when_gateway_launch_fails() -> None:
    """Gateway-image-not-built and similar errors stop startup — agents
    without a gateway cannot do their job, so we refuse to enter ready."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()

    launch_mock = AsyncMock(side_effect=RuntimeError("image missing"))
    wait_mock = AsyncMock()

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        patch("rolemesh.egress.launcher.launch_egress_gateway", launch_mock),
        patch("rolemesh.egress.launcher.wait_for_gateway_ready", wait_mock),
        pytest.raises(RuntimeError, match="image missing"),
    ):
        await main_module._ensure_container_system_running()

    wait_mock.assert_not_awaited()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_aborts_when_gateway_readiness_probe_fails() -> None:
    """Gateway binary up but not serving /healthz within budget → refuse
    to enter ready. Agents would only produce a cryptic failure later."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()

    launch_mock = AsyncMock()
    wait_mock = AsyncMock(side_effect=RuntimeError("probe exhausted"))

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        patch("rolemesh.egress.launcher.launch_egress_gateway", launch_mock),
        patch("rolemesh.egress.launcher.wait_for_gateway_ready", wait_mock),
        pytest.raises(RuntimeError, match="probe exhausted"),
    ):
        await main_module._ensure_container_system_running()

    launch_mock.assert_awaited_once()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_tolerates_runtime_without_egress_hooks() -> None:
    """Future k8s backend won't expose ensure_egress_network /
    verify_egress_gateway_reachable. hasattr() gates skip the EC-1 path
    and fall back to the pre-EC-1 surface so a minimal runtime still
    boots."""
    from rolemesh import main as main_module

    rt = MagicMock(
        spec=["ensure_available", "ensure_agent_network", "cleanup_orphans"]
    )
    rt.ensure_available = AsyncMock()
    rt.ensure_agent_network = AsyncMock()
    rt.cleanup_orphans = AsyncMock(return_value=[])

    with patch.object(main_module, "get_runtime", return_value=rt):
        await main_module._ensure_container_system_running()

    rt.ensure_available.assert_awaited_once()
    rt.ensure_agent_network.assert_awaited_once()
    rt.cleanup_orphans.assert_awaited_once()
