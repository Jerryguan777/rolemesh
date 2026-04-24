"""Tests for rolemesh.egress.launcher — gateway container orchestration.

The launcher is pure Docker-API plumbing; each happy-path assertion
targets a specific operational invariant we want to catch in review
rather than a free-form coverage number.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions
import pytest

from rolemesh.egress.launcher import (
    launch_egress_gateway,
    wait_for_gateway_ready,
)


def _docker_error(status: int, reason: str = "") -> aiodocker.exceptions.DockerError:
    return aiodocker.exceptions.DockerError(status, {"message": reason})


def _make_gateway_container() -> MagicMock:
    """Mock container handle that looks like aiodocker's DockerContainer."""
    c = MagicMock()
    c._id = "gateway-abc123"
    c.start = AsyncMock()
    c.delete = AsyncMock()
    return c


def _make_client(
    *,
    has_image: bool = True,
    has_stale: bool = False,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build a client mock with the shape launch_egress_gateway drives."""
    stale_container = MagicMock()
    stale_container.delete = AsyncMock()

    egress_network_obj = MagicMock()
    egress_network_obj.connect = AsyncMock()

    client = MagicMock()
    client.images = MagicMock()
    if has_image:
        client.images.inspect = AsyncMock()
    else:
        client.images.inspect = AsyncMock(side_effect=_docker_error(404, "image missing"))
    client.containers = MagicMock()
    if has_stale:
        client.containers.container = MagicMock(return_value=stale_container)
    else:
        client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.networks = MagicMock()
    client.networks.get = AsyncMock(return_value=egress_network_obj)
    return client, egress_network_obj, stale_container


class TestLaunchEgressGateway:
    async def test_raises_clearly_when_image_missing(self) -> None:
        """Clean error message with build instruction — catches a real
        operator mistake (forgetting to run build-egress-gateway.sh)."""
        client, _, _ = _make_client(has_image=False)

        with (
            patch(
                "rolemesh.egress.launcher._optional_env_bind",
                return_value=[],
            ),
            pytest.raises(RuntimeError, match="Egress gateway image not found"),
        ):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
                image="rolemesh-egress-gateway:latest",
            )
        # No container should have been created / deleted.
        client.containers.container.assert_not_called()

    async def test_removes_stale_gateway_before_create(self) -> None:
        """Idempotency: re-running the launcher after an orchestrator
        crash must not fail with 'container already exists'."""
        client, _egress_network_obj, stale = _make_client(has_stale=True)
        container = _make_gateway_container()
        client.containers.create_or_replace = AsyncMock(return_value=container)

        with patch("rolemesh.egress.launcher._optional_env_bind", return_value=[]):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
            )

        stale.delete.assert_awaited_once_with(force=True)

    async def test_attaches_to_both_networks_before_start(self) -> None:
        """The gateway needs both networks live from its first moment.
        Attaching after start creates a window where egress fails."""
        client, egress_network_obj, _ = _make_client()
        container = _make_gateway_container()
        captured: dict[str, Any] = {}

        async def _capture_create(name: str, config: dict[str, Any]) -> MagicMock:
            captured["config"] = config
            return container

        client.containers.create_or_replace = AsyncMock(side_effect=_capture_create)

        # Track relative order: the second network must connect BEFORE
        # container.start(). Use a shared counter — simpler than nesting
        # mocks that track call_args_list with timestamps.
        call_order: list[str] = []
        egress_network_obj.connect = AsyncMock(
            side_effect=lambda _: call_order.append("connect")  # type: ignore[func-returns-value]
        )
        container.start = AsyncMock(
            side_effect=lambda: call_order.append("start")  # type: ignore[func-returns-value]
        )

        with patch("rolemesh.egress.launcher._optional_env_bind", return_value=[]):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
            )

        assert call_order == ["connect", "start"], (
            "egress network must be attached before the gateway starts"
        )
        # Primary network in HostConfig is the agent bridge (so the
        # container's primary DNS hostname resolves on the agent bridge).
        assert captured["config"]["HostConfig"]["NetworkMode"] == "rolemesh-agent-net"
        # Restart policy survives orchestrator restarts — a dead gateway
        # causes a full-tenant outage, so Docker should auto-recover.
        assert captured["config"]["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"

    async def test_rolls_back_container_on_egress_attach_failure(self) -> None:
        """If the egress bridge attach fails, the partially-started
        container is a stale liability; remove it so the next launcher
        run doesn't trip on it."""
        client, egress_network_obj, _ = _make_client()
        container = _make_gateway_container()
        client.containers.create_or_replace = AsyncMock(return_value=container)
        egress_network_obj.connect = AsyncMock(
            side_effect=_docker_error(409, "already connected")
        )

        with (
            patch("rolemesh.egress.launcher._optional_env_bind", return_value=[]),
            pytest.raises(aiodocker.exceptions.DockerError),
        ):
            await launch_egress_gateway(
                client,
                agent_network="rolemesh-agent-net",
                egress_network="rolemesh-egress-net",
            )

        container.delete.assert_awaited_once_with(force=True)
        container.start.assert_not_called()


class TestWaitForGatewayReady:
    async def test_succeeds_when_probe_eventually_passes(self) -> None:
        """Cold-start path: first few probes fail (listener binding),
        then one succeeds. Exercised with a side_effect list."""
        calls: list[int] = []

        async def _probe_mock(*args: Any, **kwargs: Any) -> None:
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("probe failed")

        with patch(
            "rolemesh.egress.launcher.verify_egress_gateway_reachable",
            side_effect=_probe_mock,
        ):
            await wait_for_gateway_ready(
                MagicMock(),
                agent_network="rolemesh-agent-net",
                attempts=5,
                interval_s=0.0,  # no real sleep in the test
            )

        assert len(calls) == 3

    async def test_raises_when_attempts_exhausted(self) -> None:
        """When the gateway never comes up, we raise with the most
        recent probe error in the chain — preserves diagnostic detail."""
        async def _always_fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("permanent probe failure")

        with (
            patch(
                "rolemesh.egress.launcher.verify_egress_gateway_reachable",
                side_effect=_always_fail,
            ),
            pytest.raises(RuntimeError, match="did not become ready after"),
        ):
            await wait_for_gateway_ready(
                MagicMock(),
                agent_network="rolemesh-agent-net",
                attempts=3,
                interval_s=0.0,
            )
