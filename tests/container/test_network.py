"""Tests for rolemesh.container.network — agent + egress bridges and the
gateway readiness probe (EC-1).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions
import pytest

from rolemesh.container.network import (
    ensure_agent_network,
    ensure_egress_network,
    verify_egress_gateway_reachable,
)


def _docker_error(status: int, reason: str = "") -> aiodocker.exceptions.DockerError:
    return aiodocker.exceptions.DockerError(status, {"message": reason})


# ---------------------------------------------------------------------------
# ensure_agent_network
# ---------------------------------------------------------------------------


class TestEnsureAgentNetwork:
    async def test_creates_network_with_internal_and_icc_off(self) -> None:
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(side_effect=_docker_error(404, "not found"))
        client.networks.create = AsyncMock()

        await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_awaited_once()
        config = client.networks.create.await_args.kwargs["config"]
        assert config["Name"] == "rolemesh-agent-net"
        assert config["Driver"] == "bridge"
        # EC-1 invariants: the agent bridge MUST be internal and MUST disable
        # ICC. Flipping either of these silently is a regression in the
        # security posture, so each is a separate hard assertion.
        assert config["Internal"] is True, "agent bridge must be Internal=true"
        assert config["Options"]["com.docker.network.bridge.enable_icc"] == "false"

    async def test_reuses_existing_internal_network(self) -> None:
        existing = MagicMock()
        existing.show = AsyncMock(return_value={
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
            "Internal": True,
        })
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(return_value=existing)
        client.networks.create = AsyncMock()

        await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_not_awaited()

    async def test_warns_when_existing_network_is_not_internal(self) -> None:
        """Regression: a pre-EC-1 network reused as-is silently loses the
        Internal=true guarantee. Operators get a warning; they choose
        whether to recreate."""
        existing = MagicMock()
        existing.show = AsyncMock(return_value={
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
            "Internal": False,
        })
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(return_value=existing)
        client.networks.create = AsyncMock()

        with patch("rolemesh.container.network.logger") as mock_logger:
            await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_not_awaited()
        mock_logger.warning.assert_called_once()
        problems = mock_logger.warning.call_args.kwargs.get("problems", [])
        assert "not Internal" in problems

    async def test_warns_when_existing_network_has_icc_enabled(self) -> None:
        existing = MagicMock()
        existing.show = AsyncMock(return_value={
            "Options": {"com.docker.network.bridge.enable_icc": "true"},
            "Internal": True,
        })
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(return_value=existing)
        client.networks.create = AsyncMock()

        with patch("rolemesh.container.network.logger") as mock_logger:
            await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_not_awaited()
        mock_logger.warning.assert_called_once()
        problems = mock_logger.warning.call_args.kwargs.get("problems", [])
        assert "ICC enabled" in problems

    async def test_empty_network_name_skips_creation(self) -> None:
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock()
        client.networks.create = AsyncMock()

        await ensure_agent_network(client, "")

        client.networks.get.assert_not_awaited()
        client.networks.create.assert_not_awaited()

    async def test_non_404_error_propagates(self) -> None:
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(side_effect=_docker_error(500, "daemon dead"))
        client.networks.create = AsyncMock()

        with pytest.raises(aiodocker.exceptions.DockerError):
            await ensure_agent_network(client, "rolemesh-agent-net")
        client.networks.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# ensure_egress_network
# ---------------------------------------------------------------------------


class TestEnsureEgressNetwork:
    async def test_creates_plain_bridge_with_icc_off(self) -> None:
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(side_effect=_docker_error(404, "not found"))
        client.networks.create = AsyncMock()

        await ensure_egress_network(client, "rolemesh-egress-net")

        config = client.networks.create.await_args.kwargs["config"]
        assert config["Name"] == "rolemesh-egress-net"
        assert config["Driver"] == "bridge"
        # The egress bridge is the gateway's outbound path — it MUST NOT
        # be Internal, or the gateway cannot reach the public internet.
        assert config["Internal"] is False, "egress bridge must have a default route"
        # ICC off to prevent accidental neighbours on this bridge from
        # reaching the gateway directly.
        assert config["Options"]["com.docker.network.bridge.enable_icc"] == "false"

    async def test_warns_when_existing_egress_network_is_internal(self) -> None:
        """Catches the inverse regression: someone flips Internal=true on
        the egress bridge — gateway loses egress, silent failure mode."""
        existing = MagicMock()
        existing.show = AsyncMock(return_value={
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
            "Internal": True,
        })
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(return_value=existing)
        client.networks.create = AsyncMock()

        with patch("rolemesh.container.network.logger") as mock_logger:
            await ensure_egress_network(client, "rolemesh-egress-net")

        client.networks.create.assert_not_awaited()
        mock_logger.warning.assert_called_once()
        assert "Internal" in mock_logger.warning.call_args.args[0]

    async def test_empty_network_name_skips_creation(self) -> None:
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock()
        client.networks.create = AsyncMock()

        await ensure_egress_network(client, "")

        client.networks.get.assert_not_awaited()
        client.networks.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# verify_egress_gateway_reachable
# ---------------------------------------------------------------------------


def _make_probe_container(exit_code: int = 0) -> MagicMock:
    c = MagicMock()
    c.start = AsyncMock()
    c.wait = AsyncMock(return_value={"StatusCode": exit_code})
    c.delete = AsyncMock()
    c.log = AsyncMock(return_value=["probe output"])
    return c


async def test_verify_egress_gateway_reachable_success() -> None:
    probe = _make_probe_container(exit_code=0)
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.images.pull = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(return_value=probe)

    await verify_egress_gateway_reachable(
        client,
        network_name="rolemesh-agent-net",
        gateway_service_name="egress-gateway",
        reverse_proxy_port=3001,
    )

    probe.start.assert_awaited_once()
    probe.wait.assert_awaited_once()
    probe.delete.assert_awaited()


async def test_verify_egress_gateway_reachable_nonzero_exit_raises() -> None:
    probe = _make_probe_container(exit_code=1)
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.images.pull = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(return_value=probe)

    with pytest.raises(RuntimeError, match="probe failed"):
        await verify_egress_gateway_reachable(
            client,
            network_name="rolemesh-agent-net",
            gateway_service_name="egress-gateway",
            reverse_proxy_port=3001,
        )
    # Cleanup happens even on failure — catches a finally-block regression.
    probe.delete.assert_awaited()


async def test_verify_egress_gateway_reachable_empty_network_name_is_noop() -> None:
    client = MagicMock()
    client.containers = MagicMock()
    client.containers.create_or_replace = AsyncMock()

    await verify_egress_gateway_reachable(
        client,
        network_name="",
        gateway_service_name="egress-gateway",
        reverse_proxy_port=3001,
    )

    client.containers.create_or_replace.assert_not_awaited()


async def test_probe_targets_service_name_and_does_not_set_host_gateway() -> None:
    """EC-1: the probe reaches the gateway by service name via Docker
    embedded DNS, not through the host-gateway escape hatch. Catches a
    regression where someone re-adds ExtraHosts for the legacy path."""
    probe = _make_probe_container(exit_code=0)
    captured: dict[str, Any] = {}

    async def _capture_create(name: str, config: dict[str, Any]) -> MagicMock:
        captured["config"] = config
        return probe

    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(side_effect=_capture_create)

    await verify_egress_gateway_reachable(
        client,
        network_name="rolemesh-agent-net",
        gateway_service_name="egress-gateway",
        reverse_proxy_port=9999,
    )

    host_config = captured["config"]["HostConfig"]
    assert host_config["NetworkMode"] == "rolemesh-agent-net"
    # Service name + port from the caller must appear in the probe
    # command, not a hardcoded default.
    probe_cmd = " ".join(captured["config"]["Cmd"])
    assert "http://egress-gateway:9999/healthz" in probe_cmd
    # Catches: a copy-paste from the old probe that reintroduces the
    # host-gateway escape hatch. ExtraHosts must be empty or absent.
    assert not host_config.get("ExtraHosts")


async def test_probe_expects_http_200_specifically() -> None:
    """EC-1: /healthz is our own endpoint. Any non-200 is a real problem,
    unlike the pre-EC-1 probe that accepted any HTTP response."""
    probe = _make_probe_container(exit_code=0)
    captured: dict[str, Any] = {}

    async def _capture_create(name: str, config: dict[str, Any]) -> MagicMock:
        captured["config"] = config
        return probe

    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(side_effect=_capture_create)

    await verify_egress_gateway_reachable(
        client,
        network_name="rolemesh-agent-net",
        gateway_service_name="egress-gateway",
        reverse_proxy_port=3001,
    )

    probe_cmd = " ".join(captured["config"]["Cmd"])
    # The probe grep must require HTTP/1.x 200 specifically — not just
    # "HTTP/" like the legacy path.
    assert "200" in probe_cmd
    assert "HTTP/" in probe_cmd


# ---------------------------------------------------------------------------
# Fail-open regression pin for image-pull failures.
#
# Same intent as the pre-EC-1 probe: a temporary registry hiccup should
# not permanently block orchestrator startup. Caller (launcher) retries,
# and if the deeper problem persists, wait_for_gateway_ready eventually
# raises.
# ---------------------------------------------------------------------------


async def test_pull_failure_is_fail_open_not_fail_closed() -> None:
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock(side_effect=_docker_error(404, "image missing"))
    client.images.pull = AsyncMock(side_effect=_docker_error(500, "registry unreachable"))
    client.containers = MagicMock()
    client.containers.create_or_replace = AsyncMock()
    client.containers.container = MagicMock()

    with patch("rolemesh.container.network.logger") as mock_logger:
        result = await verify_egress_gateway_reachable(
            client,
            network_name="rolemesh-agent-net",
            gateway_service_name="egress-gateway",
            reverse_proxy_port=3001,
        )

    assert result is None
    client.containers.create_or_replace.assert_not_awaited()
    mock_logger.warning.assert_called_once()
    warn_args, warn_kwargs = mock_logger.warning.call_args
    assert warn_kwargs.get("image")
    assert "skipping" in warn_args[0].lower()


async def test_timeout_raises_runtime_error_and_cleans_probe() -> None:
    probe = MagicMock()
    probe.start = AsyncMock()
    probe.wait = AsyncMock(side_effect=TimeoutError())
    probe.delete = AsyncMock()
    probe.log = AsyncMock(return_value=["output"])

    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(return_value=probe)

    with pytest.raises(RuntimeError, match="timed out"):
        await verify_egress_gateway_reachable(
            client,
            network_name="rolemesh-agent-net",
            gateway_service_name="egress-gateway",
            reverse_proxy_port=3001,
            timeout_s=0.01,
        )
    probe.delete.assert_awaited()
