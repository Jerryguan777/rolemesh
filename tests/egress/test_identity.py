"""Tests for the gateway's source-IP → Identity resolver."""

from __future__ import annotations

import pytest

from rolemesh.egress.identity import Identity, IdentityResolver

pytestmark = pytest.mark.asyncio


def _started(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event": "started",
        "container_name": "rolemesh-test-123",
        "ip": "10.0.0.5",
        "tenant_id": "tenant-a",
        "coworker_id": "coworker-x",
        "user_id": "user-1",
        "conversation_id": "conv-1",
        "job_id": "job-1",
    }
    base.update(overrides)
    return base


async def test_started_then_resolve_by_ip() -> None:
    resolver = IdentityResolver()
    await resolver.handle_started(_started())
    got = resolver.resolve("10.0.0.5")
    assert isinstance(got, Identity)
    assert got.tenant_id == "tenant-a"
    assert got.coworker_id == "coworker-x"


async def test_stopped_removes_by_container() -> None:
    resolver = IdentityResolver()
    await resolver.handle_started(_started())
    await resolver.handle_stopped(
        {"event": "stopped", "container_name": "rolemesh-test-123"}
    )
    assert resolver.resolve("10.0.0.5") is None


async def test_stopped_preserves_ip_when_container_name_mismatch() -> None:
    """Docker may reassign an IP to a new container before we receive
    the stop event for the old one. The new container's started-event
    wins; the late stop for the old container must not clobber it."""
    resolver = IdentityResolver()
    await resolver.handle_started(_started())
    await resolver.handle_started(
        _started(container_name="rolemesh-new-456", job_id="job-2")
    )
    # The new container took over IP 10.0.0.5.
    # A late stop for the old container should be a no-op (the IP now
    # points at a different container).
    await resolver.handle_stopped(
        {"event": "stopped", "container_name": "rolemesh-test-123"}
    )
    got = resolver.resolve("10.0.0.5")
    assert got is not None
    assert got.container_name == "rolemesh-new-456"


async def test_malformed_stopped_event_is_silently_dropped() -> None:
    """Regression: handle_stopped used to pass ``event=event`` as a
    structlog kwarg, colliding with structlog's message slot and
    raising TypeError inside the warning log. nats-py would eat the
    exception, so malformed stopped events would disappear without a
    trace. Now we pass the event under ``payload``.
    """
    resolver = IdentityResolver()
    # No container_name in the event → triggers the warn branch.
    await resolver.handle_stopped({"event": "stopped"})
    # Must not raise; nothing should be registered.
    assert resolver.by_container == {}


async def test_malformed_started_event_is_ignored() -> None:
    """Fail-safe: missing required keys drop the event, not crash
    the resolver."""
    resolver = IdentityResolver()
    await resolver.handle_started({"event": "started"})  # missing everything
    assert resolver.resolve("10.0.0.5") is None


async def test_unknown_ip_resolves_to_none() -> None:
    """Fail-closed: unknown source IP never defaults to a tenant."""
    resolver = IdentityResolver()
    assert resolver.resolve("203.0.113.1") is None
