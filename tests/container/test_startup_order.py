"""Startup contract for ``main._ensure_container_system_running``.

Declarative infrastructure (docs/21 §1): the deployment layer owns
networks + gateway + NATS; the orchestrator only VERIFIES them at
startup via ``ContainerRuntime.verify_infrastructure`` and refuses to
start when an invariant does not hold. The old contract ("gateway must
launch after the NATS responders") is gone — the compose-managed
gateway starts degraded and seeds its snapshot via a retry loop, so
the orchestrator has no launch step at all.

Invariants pinned here:

  1. Startup calls verify_infrastructure exactly once, after
     ensure_available and before cleanup_orphans.
  2. A verify_infrastructure failure propagates (fail-closed)
     and prevents every downstream step — the orchestrator must not
     enter ready state on unverified infrastructure.
  3. ensure_available failure short-circuits everything, including
     verification.
  4. An empty bridge name is a hard configuration error raised
     BEFORE any verification.

The runtime is mocked at the ContainerRuntime boundary; correctness of
verify_infrastructure itself is covered by
test_verify_infrastructure.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_runtime_mock() -> MagicMock:
    rt = MagicMock()
    rt.ensure_available = AsyncMock()
    rt.verify_infrastructure = AsyncMock()
    rt.cleanup_orphans = AsyncMock(return_value=[])
    return rt


async def test_startup_verifies_infrastructure_in_order() -> None:
    """ensure_available → verify_infrastructure → cleanup_orphans,
    nothing else. Verification must precede orphan cleanup so a broken
    deployment is reported before we start mutating container state."""
    from rolemesh import main as main_module

    parent = MagicMock()
    rt = _make_runtime_mock()
    parent.attach_mock(rt.ensure_available, "ensure_available")
    parent.attach_mock(rt.verify_infrastructure, "verify_infrastructure")
    parent.attach_mock(rt.cleanup_orphans, "cleanup_orphans")

    with patch.object(main_module, "get_runtime", return_value=rt):
        await main_module._ensure_container_system_running()

    call_order = [c[0] for c in parent.mock_calls if c[0]]
    assert call_order == [
        "ensure_available",
        "verify_infrastructure",
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

    rt.verify_infrastructure.assert_not_awaited()
    rt.cleanup_orphans.assert_not_awaited()


async def test_startup_fail_closed_when_verification_fails() -> None:
    """verify_infrastructure raising must abort startup — the
    orchestrator refuses to run against undeclared/broken infrastructure
    instead of degrading or self-repairing."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()
    rt.verify_infrastructure = AsyncMock(
        side_effect=RuntimeError("agent network does not exist")
    )

    with (
        patch.object(main_module, "get_runtime", return_value=rt),
        pytest.raises(RuntimeError, match="agent network does not exist"),
    ):
        await main_module._ensure_container_system_running()

    rt.ensure_available.assert_awaited_once()
    rt.cleanup_orphans.assert_not_awaited()


async def test_empty_bridge_name_refuses_to_start() -> None:
    """An empty bridge name is a hard config error — there's no
    Internal=true bridge to enforce isolation on, and egress control
    has no off-switch (docs/21 §1). Raised before any verification
    attempt so the message points at the config, not at a 'missing
    network'; cleanup must not run either."""
    from rolemesh import main as main_module

    rt = _make_runtime_mock()

    with (
        patch.object(main_module, "CONTAINER_NETWORK_NAME", ""),
        patch.object(main_module, "get_runtime", return_value=rt),
        pytest.raises(RuntimeError, match="CONTAINER_NETWORK_NAME"),
    ):
        await main_module._ensure_container_system_running()

    rt.verify_infrastructure.assert_not_awaited()
    rt.cleanup_orphans.assert_not_awaited()
