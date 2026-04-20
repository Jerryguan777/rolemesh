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


# ---------------------------------------------------------------------------
# Fail-open regression pin
#
# When the probe image cannot be pulled (offline CI, restricted registry,
# image name typo), verify_proxy_reachable currently SKIPS the connectivity
# check and returns None instead of raising. That is an *intentional* design
# choice — a temporary registry hiccup should not block orchestrator startup.
#
# This test exists to make the choice explicit and auditable. If someone
# changes the behaviour to fail-closed (raise) the test fails and they have
# to re-justify the change. If someone removes the warning log the test
# fails and the silent-skip becomes observable to operators.
#
# Changing the design to fail-closed is fine — just update this test to match
# and document why the tradeoff flipped.
# ---------------------------------------------------------------------------


async def test_pull_failure_is_fail_open_not_fail_closed() -> None:
    """PIN: When the probe image can't be pulled, verify_proxy_reachable
    MUST NOT raise, MUST skip creating the probe container, and MUST emit
    a warning log by image name."""
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock(side_effect=_docker_error(404, "image missing"))
    client.images.pull = AsyncMock(side_effect=_docker_error(500, "registry unreachable"))
    client.containers = MagicMock()
    client.containers.create_or_replace = AsyncMock()
    client.containers.container = MagicMock()

    with patch("rolemesh.container.network.logger") as mock_logger:
        # Must not raise — this is the fail-open contract.
        result = await verify_proxy_reachable(client, "rolemesh-agent-net", 3001)

    assert result is None
    # The probe container must not have been created — that's the whole
    # point of the skip.
    client.containers.create_or_replace.assert_not_awaited()
    # The skip must be observable to operators via a structured warning,
    # otherwise a silent gap appears in the hardening self-check.
    mock_logger.warning.assert_called_once()
    warn_args, warn_kwargs = mock_logger.warning.call_args
    assert warn_kwargs.get("image")  # image name in the log
    # The message must mention "skipping" or similar so log aggregators
    # can alert on the gap.
    assert "skipping" in warn_args[0].lower()


async def test_timeout_raises_runtime_error_and_cleans_probe() -> None:
    """If the probe hangs past `timeout_s` we must abort orchestrator startup
    (RuntimeError) AND still delete the probe container so the next run
    doesn't trip on a stale name. The cleanup happens in the finally block
    — easy to accidentally drop during a refactor."""
    probe = MagicMock()
    probe.start = AsyncMock()
    # Simulate a hung container by having wait() raise TimeoutError when
    # awaited (mimics what asyncio.wait_for surfaces on timeout).
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
        await verify_proxy_reachable(
            client, "rolemesh-agent-net", 3001, timeout_s=0.01,
        )
    # Cleanup must happen even when the probe path raised.
    probe.delete.assert_awaited()


async def test_pull_failure_does_not_leak_stale_probe() -> None:
    """Sanity: if we skip probe creation, we also must not leave a stale
    container around from a previous run's ID lookup."""
    stale = MagicMock()
    stale.delete = AsyncMock()
    client = MagicMock()
    client.images = MagicMock()
    client.images.inspect = AsyncMock(side_effect=_docker_error(404, "image missing"))
    client.images.pull = AsyncMock(side_effect=_docker_error(500, "registry unreachable"))
    client.containers = MagicMock()
    client.containers.container = MagicMock(return_value=stale)
    client.containers.create_or_replace = AsyncMock()

    await verify_proxy_reachable(client, "rolemesh-agent-net", 3001)

    # With the current implementation we bail before the stale-wipe step;
    # pin that so future refactors that reorder the steps don't accidentally
    # touch container state after having decided to skip.
    stale.delete.assert_not_awaited()
