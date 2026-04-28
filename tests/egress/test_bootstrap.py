"""Tests for ``rolemesh.egress.bootstrap.ensure_gateway_running_and_register_dns``.

The helper is the single shared entry point that every code path
spinning up a ``ContainerRuntime`` must call so spawned agent
containers get the gateway pinned as their DNS resolver. The
production-blocking bug it fixes (eval CLI silently inheriting an
unset ``_EGRESS_GATEWAY_DNS_IP`` and falling back to Docker's
default resolver) makes the contract specifically:

* Reuse a running gateway rather than recreating it (so concurrent
  callers like the orchestrator daemon plus an eval CLI don't
  disrupt each other).
* Register the discovered IP via ``set_egress_gateway_dns_ip``.
* Return None silently when EC-2 is inactive (rollback mode / k8s
  runtime) — those callers don't need a gateway and registering
  None would mask real config errors.
* Be tolerant of inspect-format anomalies (running container with
  no agent-net IP recorded): log an error, fall through to None,
  do NOT raise — agents can still spawn, they just lose DNS-level
  egress filtering.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiodocker.exceptions
import pytest


@pytest.fixture
def _runner_module() -> Any:
    """Import ``rolemesh.container.runner`` via a path that does NOT
    re-trigger its own circular import.

    ``runner`` imports ``rolemesh.agent.executor`` which transitively
    imports ``container_executor`` which tries to pull
    ``build_container_spec`` back out of ``runner`` — running fine in
    production where main.py orchestrates the import order, but
    blowing up if a test tries to import ``runner`` directly first
    because ``container_executor`` runs before ``runner`` finishes
    defining its symbols.

    Loading ``rolemesh.agent.container_executor`` first defers the
    inner import to a moment where it can be served from sys.modules
    without entering an unfinished module body. Once that's done,
    ``rolemesh.container.runner`` is fully loaded for the test's
    ``patch.object`` calls.
    """
    import rolemesh.agent.container_executor  # noqa: F401
    import rolemesh.container.runner as runner

    return runner


def _docker_error(status: int, msg: str = "") -> aiodocker.exceptions.DockerError:
    return aiodocker.exceptions.DockerError(status, {"message": msg})


def _make_runtime(*, ec2_capable: bool = True) -> MagicMock:
    """A runtime mock that ``_ec2_active`` will accept (or reject)."""
    runtime = MagicMock()
    runtime._ensure_client = MagicMock(return_value=MagicMock())
    if ec2_capable:
        runtime.ensure_egress_network = MagicMock()
        runtime.verify_egress_gateway_reachable = MagicMock()
    else:
        # ``hasattr`` is what ``_ec2_active`` checks, so we have to
        # actively delete the attributes for the negative test —
        # MagicMock auto-creates them on access.
        del runtime.ensure_egress_network
        del runtime.verify_egress_gateway_reachable
    return runtime


def _make_gateway_inspect(
    *,
    running: bool = True,
    ip: str | None = "172.18.0.7",
    network_name: str = "rolemesh-agent-net",
) -> dict[str, Any]:
    """Mimic the shape ``aiodocker.DockerContainer.show()`` returns."""
    networks: dict[str, Any]
    if ip is not None:
        networks = {network_name: {"IPAddress": ip}}
    else:
        networks = {network_name: {}}  # exists but empty
    return {
        "State": {"Running": running},
        "NetworkSettings": {"Networks": networks},
    }


def _wire_existing_gateway(
    runtime: MagicMock, inspect_dict: dict[str, Any]
) -> MagicMock:
    """Install a containers.container() mock that returns an inspectable
    gateway. Returns the inner container mock so tests can verify
    delete/start were not called.
    """
    container = MagicMock()
    container.show = AsyncMock(return_value=inspect_dict)
    runtime._ensure_client.return_value.containers = MagicMock()
    runtime._ensure_client.return_value.containers.container = MagicMock(
        return_value=container
    )
    return container


def _wire_no_existing_gateway(runtime: MagicMock) -> None:
    """Make the first ``containers.container(...)`` raise — the helper
    should fall through to launch.
    """
    runtime._ensure_client.return_value.containers = MagicMock()
    runtime._ensure_client.return_value.containers.container = MagicMock(
        side_effect=_docker_error(404, "no such container")
    )


# ---------------------------------------------------------------------------
# EC-2 inactive — early return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_none_when_runtime_lacks_ec2_methods() -> None:
    """k8s runtime / dev runtime without egress methods returns
    silently — registering None would shadow a real misconfiguration.
    """
    from rolemesh.egress.bootstrap import ensure_gateway_running_and_register_dns

    runtime = _make_runtime(ec2_capable=False)
    with patch(
        "rolemesh.egress.bootstrap.CONTAINER_NETWORK_NAME", "rolemesh-agent-net"
    ):
        result = await ensure_gateway_running_and_register_dns(runtime)

    assert result is None
    runtime._ensure_client.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_container_network_name_unset() -> None:
    """Rollback mode (operator turned EC off) — no-op."""
    from rolemesh.egress.bootstrap import ensure_gateway_running_and_register_dns

    runtime = _make_runtime(ec2_capable=True)
    with patch("rolemesh.egress.bootstrap.CONTAINER_NETWORK_NAME", ""):
        result = await ensure_gateway_running_and_register_dns(runtime)

    assert result is None
    runtime._ensure_client.assert_not_called()


# ---------------------------------------------------------------------------
# Reuse-if-running (the path that fixes the eval-CLI duplication bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reuses_running_gateway_without_relaunch(_runner_module: Any) -> None:
    """The single most important assertion in this file.

    A second caller (e.g. eval CLI starting up alongside the
    orchestrator daemon) must NOT recreate the gateway — that would
    tear down a working gateway in active use by other processes.
    """
    from rolemesh.egress import bootstrap

    runtime = _make_runtime()
    _wire_existing_gateway(runtime, _make_gateway_inspect(running=True))

    with (
        patch.object(bootstrap, "CONTAINER_NETWORK_NAME", "rolemesh-agent-net"),
        patch.object(bootstrap, "launch_egress_gateway", AsyncMock()) as launch_mock,
        patch.object(bootstrap, "wait_for_gateway_ready", AsyncMock()),
        patch.object(
            _runner_module, "set_egress_gateway_dns_ip"
        ) as set_dns_mock,
    ):
        result = await bootstrap.ensure_gateway_running_and_register_dns(runtime)

    assert result == "172.18.0.7"
    set_dns_mock.assert_called_once_with("172.18.0.7")
    launch_mock.assert_not_called(), (
        "must not relaunch when an existing healthy gateway is found"
    )


@pytest.mark.asyncio
async def test_reuse_path_still_verifies_reachability(_runner_module: Any) -> None:
    """A 'Running' container could be paused or have a broken
    /healthz responder. Reuse-fast-path still polls reachability,
    suppressing exceptions so a transient blip doesn't block
    registration of an otherwise-good IP.
    """
    from rolemesh.egress import bootstrap

    runtime = _make_runtime()
    _wire_existing_gateway(runtime, _make_gateway_inspect(running=True))

    with (
        patch.object(bootstrap, "CONTAINER_NETWORK_NAME", "rolemesh-agent-net"),
        patch.object(bootstrap, "launch_egress_gateway", AsyncMock()),
        patch.object(
            bootstrap, "wait_for_gateway_ready", AsyncMock()
        ) as wait_mock,
        patch.object(_runner_module, "set_egress_gateway_dns_ip"),
    ):
        await bootstrap.ensure_gateway_running_and_register_dns(runtime)

    wait_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Launch path (no gateway running)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launches_when_no_existing_gateway(_runner_module: Any) -> None:
    from rolemesh.egress import bootstrap

    runtime = _make_runtime()

    # First call: container() raises 404. Second call (post-launch):
    # returns a freshly-spawned container. Track call count to switch
    # behaviour mid-test without re-wiring mocks.
    fresh_container = MagicMock()
    fresh_container.show = AsyncMock(
        return_value=_make_gateway_inspect(running=True, ip="172.18.0.42")
    )
    container_calls: list[Any] = []

    def _container_factory(name: str) -> Any:
        container_calls.append(name)
        if len(container_calls) == 1:
            raise _docker_error(404, "no such container")
        return fresh_container

    runtime._ensure_client.return_value.containers = MagicMock()
    runtime._ensure_client.return_value.containers.container = MagicMock(
        side_effect=_container_factory
    )

    with (
        patch.object(bootstrap, "CONTAINER_NETWORK_NAME", "rolemesh-agent-net"),
        patch.object(bootstrap, "launch_egress_gateway", AsyncMock()) as launch_mock,
        patch.object(bootstrap, "wait_for_gateway_ready", AsyncMock()) as wait_mock,
        patch.object(
            _runner_module, "set_egress_gateway_dns_ip"
        ) as set_dns_mock,
    ):
        result = await bootstrap.ensure_gateway_running_and_register_dns(runtime)

    assert result == "172.18.0.42"
    launch_mock.assert_called_once()
    wait_mock.assert_called_once()
    set_dns_mock.assert_called_once_with("172.18.0.42")


@pytest.mark.asyncio
async def test_treats_stopped_container_as_no_gateway(_runner_module: Any) -> None:
    """A container in 'Exited' state is unusable — fall through to
    launch rather than registering its (stale) IP.
    """
    from rolemesh.egress import bootstrap

    runtime = _make_runtime()

    stopped = MagicMock()
    stopped.show = AsyncMock(
        return_value=_make_gateway_inspect(running=False)
    )
    fresh = MagicMock()
    fresh.show = AsyncMock(
        return_value=_make_gateway_inspect(running=True, ip="172.18.0.99")
    )
    calls: list[int] = [0]

    def _factory(name: str) -> Any:
        calls[0] += 1
        return stopped if calls[0] == 1 else fresh

    runtime._ensure_client.return_value.containers = MagicMock()
    runtime._ensure_client.return_value.containers.container = MagicMock(
        side_effect=_factory
    )

    with (
        patch.object(bootstrap, "CONTAINER_NETWORK_NAME", "rolemesh-agent-net"),
        patch.object(bootstrap, "launch_egress_gateway", AsyncMock()) as launch_mock,
        patch.object(bootstrap, "wait_for_gateway_ready", AsyncMock()),
        patch.object(_runner_module, "set_egress_gateway_dns_ip"),
    ):
        result = await bootstrap.ensure_gateway_running_and_register_dns(runtime)

    assert result == "172.18.0.99"
    launch_mock.assert_called_once(), (
        "stopped container should trigger fresh launch, not be reused"
    )


# ---------------------------------------------------------------------------
# Inspect-output anomalies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_launch_missing_ip_logs_error_and_returns_none(_runner_module: Any) -> None:
    """Healthy /healthz but no IPAddress on agent-net is the worst
    realistic case: the gateway answers but inspect can't tell us
    its IP. Log an error and return None — agents will fall back
    to Docker DNS, which is "broken-but-not-down".
    """
    from rolemesh.egress import bootstrap

    runtime = _make_runtime()

    fresh = MagicMock()
    fresh.show = AsyncMock(
        return_value=_make_gateway_inspect(running=True, ip=None)
    )
    calls: list[int] = [0]

    def _factory(name: str) -> Any:
        calls[0] += 1
        # Force the launch path so we exercise the post-launch
        # inspect: first call raises (no existing), second returns
        # the no-IP inspect.
        if calls[0] == 1:
            raise _docker_error(404, "no such container")
        return fresh

    runtime._ensure_client.return_value.containers = MagicMock()
    runtime._ensure_client.return_value.containers.container = MagicMock(
        side_effect=_factory
    )

    with (
        patch.object(bootstrap, "CONTAINER_NETWORK_NAME", "rolemesh-agent-net"),
        patch.object(bootstrap, "launch_egress_gateway", AsyncMock()),
        patch.object(bootstrap, "wait_for_gateway_ready", AsyncMock()),
        patch.object(
            _runner_module, "set_egress_gateway_dns_ip"
        ) as set_dns_mock,
    ):
        result = await bootstrap.ensure_gateway_running_and_register_dns(runtime)

    assert result is None
    set_dns_mock.assert_not_called(), (
        "must not register an empty/None IP — caller would silently "
        "lose DNS protection without a config error to debug"
    )


@pytest.mark.asyncio
async def test_inspect_format_anomaly_falls_through_to_launch(_runner_module: Any) -> None:
    """Reuse path: existing container's inspect output has a missing
    IPAddress field. We treat this as 'no usable gateway' and fall
    through to the launch path (rather than registering None).
    """
    from rolemesh.egress import bootstrap

    runtime = _make_runtime()

    weird = MagicMock()
    weird.show = AsyncMock(
        return_value=_make_gateway_inspect(running=True, ip=None)
    )
    fresh = MagicMock()
    fresh.show = AsyncMock(
        return_value=_make_gateway_inspect(running=True, ip="172.18.0.7")
    )
    calls: list[int] = [0]

    def _factory(name: str) -> Any:
        calls[0] += 1
        return weird if calls[0] == 1 else fresh

    runtime._ensure_client.return_value.containers = MagicMock()
    runtime._ensure_client.return_value.containers.container = MagicMock(
        side_effect=_factory
    )

    with (
        patch.object(bootstrap, "CONTAINER_NETWORK_NAME", "rolemesh-agent-net"),
        patch.object(bootstrap, "launch_egress_gateway", AsyncMock()) as launch_mock,
        patch.object(bootstrap, "wait_for_gateway_ready", AsyncMock()),
        patch.object(_runner_module, "set_egress_gateway_dns_ip"),
    ):
        result = await bootstrap.ensure_gateway_running_and_register_dns(runtime)

    assert result == "172.18.0.7"
    launch_mock.assert_called_once(), (
        "missing IP in existing container's inspect should trigger "
        "a fresh launch, not register a None IP"
    )
