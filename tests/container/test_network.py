"""Tests for rolemesh.container.network — agent bridge setup + connectivity probe."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions
import pytest

from rolemesh.container.network import ensure_agent_network, verify_proxy_reachable


def _docker_error(status: int, reason: str = "") -> aiodocker.exceptions.DockerError:
    return aiodocker.exceptions.DockerError(status, {"message": reason})


# ---------------------------------------------------------------------------
# ensure_agent_network
# ---------------------------------------------------------------------------


class TestEnsureAgentNetwork:
    async def test_creates_network_when_absent(self) -> None:
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(side_effect=_docker_error(404, "not found"))
        client.networks.create = AsyncMock()

        await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_awaited_once()
        config = client.networks.create.await_args.kwargs["config"]
        assert config["Name"] == "rolemesh-agent-net"
        assert config["Driver"] == "bridge"
        # The whole point of this network: ICC off.
        assert config["Options"]["com.docker.network.bridge.enable_icc"] == "false"

    async def test_reuses_existing_network_with_icc_off(self) -> None:
        existing = MagicMock()
        existing.show = AsyncMock(return_value={
            "Options": {"com.docker.network.bridge.enable_icc": "false"},
        })
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(return_value=existing)
        client.networks.create = AsyncMock()

        await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_not_awaited()

    async def test_warns_when_existing_network_has_icc_enabled(self) -> None:
        existing = MagicMock()
        existing.show = AsyncMock(return_value={
            "Options": {"com.docker.network.bridge.enable_icc": "true"},
        })
        client = MagicMock()
        client.networks = MagicMock()
        client.networks.get = AsyncMock(return_value=existing)
        client.networks.create = AsyncMock()

        with patch("rolemesh.container.network.logger") as mock_logger:
            await ensure_agent_network(client, "rolemesh-agent-net")

        client.networks.create.assert_not_awaited()
        mock_logger.warning.assert_called_once()
        assert "ICC enabled" in mock_logger.warning.call_args.args[0]

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
# verify_proxy_reachable
# ---------------------------------------------------------------------------


def _make_probe_container(exit_code: int = 0) -> MagicMock:
    c = MagicMock()
    c.start = AsyncMock()
    c.wait = AsyncMock(return_value={"StatusCode": exit_code})
    c.delete = AsyncMock()
    c.log = AsyncMock(return_value=["probe output"])
    return c


async def test_verify_proxy_reachable_success() -> None:
    probe = _make_probe_container(exit_code=0)
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.images.pull = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(return_value=probe)

    await verify_proxy_reachable(client, "rolemesh-agent-net", 3001)

    probe.start.assert_awaited_once()
    probe.wait.assert_awaited_once()
    probe.delete.assert_awaited()


async def test_verify_proxy_reachable_nonzero_exit_raises() -> None:
    probe = _make_probe_container(exit_code=1)
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock()
    client.images.pull = AsyncMock()
    client.containers = MagicMock()
    client.containers.container = MagicMock(side_effect=_docker_error(404, "no stale"))
    client.containers.create_or_replace = AsyncMock(return_value=probe)

    with pytest.raises(RuntimeError, match="connectivity probe failed"):
        await verify_proxy_reachable(client, "rolemesh-agent-net", 3001)
    # Probe container must always be cleaned up, even on failure.
    probe.delete.assert_awaited()


async def test_verify_proxy_reachable_empty_network_name_is_noop() -> None:
    client = MagicMock()
    client.containers = MagicMock()
    client.containers.create_or_replace = AsyncMock()

    await verify_proxy_reachable(client, "", 3001)

    client.containers.create_or_replace.assert_not_awaited()


async def test_verify_proxy_reachable_attaches_to_correct_network() -> None:
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

    await verify_proxy_reachable(client, "rolemesh-agent-net", 9999)

    assert captured["config"]["HostConfig"]["NetworkMode"] == "rolemesh-agent-net"
    # Port from the caller must be in the probe command, not a hardcoded default.
    probe_cmd = " ".join(captured["config"]["Cmd"])
    assert ":9999/" in probe_cmd
    # Host-gateway ExtraHost is mandatory — that's how the probe routes out
    # of a custom bridge. If this entry disappears, the probe will fail
    # silently on Linux and we lose the entire self-check guarantee.
    assert "host.docker.internal:host-gateway" in captured["config"]["HostConfig"]["ExtraHosts"]
