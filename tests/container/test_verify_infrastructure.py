"""DockerRuntime.verify_infrastructure — fail-closed verification of the
compose-declared infrastructure (design docs/21 §4.2).

Spec under test (not the implementation): the deployment layer promises

  (a) agent network exists and is Internal=true,
  (b) egress network exists,
  (c) the gateway container holds EGRESS_GATEWAY_DNS_IP on agent-net,
  (d) the gateway answers GET /healthz with 200,
  (e) NATS is TCP-reachable at NATS_URL,

and verify_infrastructure must raise — with a message telling the
operator how to fix the deployment — whenever any single promise is
broken, while a fully-healthy topology passes. The check is read-only:
it must never create or repair anything.

Mock boundary: only aiodocker (fake client objects). HTTP and TCP are
exercised against REAL local listeners so the success path proves the
actual aiohttp / asyncio code works, not a mock of it.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import aiodocker.exceptions
import pytest
from aiohttp import web

from rolemesh.container import docker_runtime
from rolemesh.container.docker_runtime import DockerRuntime
from rolemesh.core import config

AGENT_NET = "rolemesh-agent-net"
EGRESS_NET = "rolemesh-egress-net"
GATEWAY = "egress-gateway"

_FIX_HINT = "docker compose -f deploy/compose/compose.yaml up -d"


# ---------------------------------------------------------------------------
# aiodocker boundary fakes. Deliberately minimal: they expose ONLY the
# read-side surface (networks.get/show, containers.container/show). If a
# regression makes verify_infrastructure try to create/repair anything,
# the missing attribute raises — locking the read-only contract.
# ---------------------------------------------------------------------------


class _FakeNetwork:
    def __init__(self, info: dict[str, Any]) -> None:
        self._info = info

    async def show(self) -> dict[str, Any]:
        return self._info


class _FakeNetworks:
    def __init__(self, nets: dict[str, dict[str, Any]]) -> None:
        self._nets = nets

    async def get(self, name: str) -> _FakeNetwork:
        if name not in self._nets:
            raise aiodocker.exceptions.DockerError(
                404, {"message": f"network {name} not found"}
            )
        return _FakeNetwork(self._nets[name])


class _FakeContainer:
    def __init__(self, info: dict[str, Any] | None) -> None:
        self._info = info

    async def show(self) -> dict[str, Any]:
        if self._info is None:
            raise aiodocker.exceptions.DockerError(
                404, {"message": "no such container"}
            )
        return self._info


class _FakeContainers:
    def __init__(self, containers: dict[str, dict[str, Any]]) -> None:
        self._containers = containers
        self.inspect_calls = 0

    def container(self, name: str) -> _FakeContainer:
        self.inspect_calls += 1
        return _FakeContainer(self._containers.get(name))


class _FakeClient:
    def __init__(
        self,
        nets: dict[str, dict[str, Any]],
        containers: dict[str, dict[str, Any]],
    ) -> None:
        self.networks = _FakeNetworks(nets)
        self.containers = _FakeContainers(containers)


def _healthy_networks() -> dict[str, dict[str, Any]]:
    return {
        AGENT_NET: {"Name": AGENT_NET, "Internal": True},
        EGRESS_NET: {"Name": EGRESS_NET, "Internal": False},
    }


def _gateway_info(ip: str, network: str = AGENT_NET) -> dict[str, Any]:
    return {"NetworkSettings": {"Networks": {network: {"IPAddress": ip}}}}


def _runtime_with(client: _FakeClient) -> DockerRuntime:
    rt = DockerRuntime()
    rt._client = client  # type: ignore[assignment]
    return rt


def _free_port() -> int:
    """A port that was free a moment ago — used as a 'nothing listens
    here' target."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Real local listeners for the HTTP / TCP legs.
# ---------------------------------------------------------------------------


class _HealthzServer:
    """Real aiohttp server for /healthz; per-request status via a list
    so tests can model 'starting up, then healthy'."""

    def __init__(self, statuses: list[int]) -> None:
        self._statuses = statuses
        self.port: int = 0
        self._runner: web.AppRunner | None = None

    async def _handler(self, _request: web.Request) -> web.Response:
        status = self._statuses[0] if len(self._statuses) == 1 else self._statuses.pop(0)
        return web.Response(status=status, text="ok")

    async def __aenter__(self) -> _HealthzServer:
        app = web.Application()
        app.router.add_get("/healthz", self._handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._runner is not None
        await self._runner.cleanup()


class _TcpServer:
    """Real TCP listener standing in for NATS."""

    def __init__(self) -> None:
        self.port: int = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _TcpServer:
        self._server = await asyncio.start_server(
            lambda r, w: w.close(), "127.0.0.1", 0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


@pytest.fixture
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the retry budget so failure cases don't burn 60s, while
    keeping >1 attempt possible (retry behaviour stays observable)."""
    monkeypatch.setattr(docker_runtime, "_VERIFY_RETRY_BUDGET_S", 0.3)
    monkeypatch.setattr(docker_runtime, "_VERIFY_RETRY_INTERVAL_S", 0.05)


def _patch_topology_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dns_ip: str = "127.0.0.1",
    healthz_port: int | None = None,
    nats_port: int | None = None,
) -> None:
    """Point the config the verifier reads at the local test listeners.

    ``ROLEMESH_HOST_DATA_DIR`` is pinned to "" because these cases test
    invariants (a)-(e) only; the DooD self-check leg has its own suite
    (test_dood_translation.py). Without the pin the tests are not
    hermetic: a developer ``.env`` that sets the variable leaks in via
    ``rolemesh.bootstrap``'s load_dotenv whenever another test module
    in the same worker imported rolemesh.main/webui.main before
    core.config — and the success-path tests then attempt a real DooD
    probe against the fake docker client.
    """
    monkeypatch.setattr(config, "ROLEMESH_HOST_DATA_DIR", "")
    monkeypatch.setattr(config, "CONTAINER_NETWORK_NAME", AGENT_NET)
    monkeypatch.setattr(config, "CONTAINER_EGRESS_NETWORK_NAME", EGRESS_NET)
    monkeypatch.setattr(config, "EGRESS_GATEWAY_CONTAINER_NAME", GATEWAY)
    monkeypatch.setattr(config, "EGRESS_GATEWAY_DNS_IP", dns_ip)
    if healthz_port is not None:
        monkeypatch.setattr(config, "CREDENTIAL_PROXY_PORT", healthz_port)
    if nats_port is not None:
        monkeypatch.setattr(config, "NATS_URL", f"nats://127.0.0.1:{nats_port}")


# ---------------------------------------------------------------------------
# Happy path — everything the deployment layer promised is true.
# ---------------------------------------------------------------------------


async def test_all_invariants_hold_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _HealthzServer([200]) as http, _TcpServer() as tcp:
        _patch_topology_config(
            monkeypatch, healthz_port=http.port, nats_port=tcp.port
        )
        client = _FakeClient(
            _healthy_networks(), {GATEWAY: _gateway_info("127.0.0.1")}
        )
        await _runtime_with(client).verify_infrastructure()  # must not raise


async def test_transient_gateway_cold_start_is_absorbed_by_retry(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    """compose starts the gateway before the orchestrator, but 'started'
    != 'serving': the first healthz probes may fail while Python is
    still importing. The verifier must retry within its budget instead
    of failing the boot on the first 503."""
    async with _HealthzServer([503, 503, 200]) as http, _TcpServer() as tcp:
        _patch_topology_config(
            monkeypatch, healthz_port=http.port, nats_port=tcp.port
        )
        client = _FakeClient(
            _healthy_networks(), {GATEWAY: _gateway_info("127.0.0.1")}
        )
        await _runtime_with(client).verify_infrastructure()  # must not raise


# ---------------------------------------------------------------------------
# (a) agent network
# ---------------------------------------------------------------------------


async def test_missing_agent_network_fails_with_fix_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_topology_config(monkeypatch)
    nets = _healthy_networks()
    del nets[AGENT_NET]
    client = _FakeClient(nets, {GATEWAY: _gateway_info("127.0.0.1")})

    with pytest.raises(RuntimeError) as exc:
        await _runtime_with(client).verify_infrastructure()
    assert AGENT_NET in str(exc.value)
    assert _FIX_HINT in str(exc.value)


async def test_agent_network_internal_false_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation guard: a network that exists but is NOT Internal=true
    gives agents a direct route to the internet. 'Exists' alone must
    never pass the check."""
    _patch_topology_config(monkeypatch)
    nets = _healthy_networks()
    nets[AGENT_NET]["Internal"] = False
    client = _FakeClient(nets, {GATEWAY: _gateway_info("127.0.0.1")})

    with pytest.raises(RuntimeError, match="Internal"):
        await _runtime_with(client).verify_infrastructure()


async def test_agent_network_missing_internal_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network inspect payload without an 'Internal' key must be
    treated as not-internal (fail-closed), not defaulted to true."""
    _patch_topology_config(monkeypatch)
    nets = _healthy_networks()
    del nets[AGENT_NET]["Internal"]
    client = _FakeClient(nets, {GATEWAY: _gateway_info("127.0.0.1")})

    with pytest.raises(RuntimeError, match="Internal"):
        await _runtime_with(client).verify_infrastructure()


# ---------------------------------------------------------------------------
# (b) egress network
# ---------------------------------------------------------------------------


async def test_missing_egress_network_fails_with_fix_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_topology_config(monkeypatch)
    nets = _healthy_networks()
    del nets[EGRESS_NET]
    client = _FakeClient(nets, {GATEWAY: _gateway_info("127.0.0.1")})

    with pytest.raises(RuntimeError) as exc:
        await _runtime_with(client).verify_infrastructure()
    assert EGRESS_NET in str(exc.value)
    assert _FIX_HINT in str(exc.value)


# ---------------------------------------------------------------------------
# (c) gateway address
# ---------------------------------------------------------------------------


async def test_gateway_container_missing_fails_after_retries(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    _patch_topology_config(monkeypatch)
    client = _FakeClient(_healthy_networks(), {})

    with pytest.raises(RuntimeError) as exc:
        await _runtime_with(client).verify_infrastructure()
    assert GATEWAY in str(exc.value)
    assert _FIX_HINT in str(exc.value)
    # The container check is allowed to race compose cold start, so it
    # must have retried rather than failing on the first inspect.
    assert client.containers.inspect_calls > 1


async def test_gateway_ip_mismatch_names_both_addresses(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    """If the running gateway's address drifts from the configured
    EGRESS_GATEWAY_DNS_IP, every agent spawn would pin a dead DNS
    resolver — the error must surface both values so the operator can
    tell config drift from compose drift."""
    _patch_topology_config(monkeypatch, dns_ip="127.0.0.1")
    client = _FakeClient(
        _healthy_networks(), {GATEWAY: _gateway_info("172.28.100.99")}
    )

    with pytest.raises(RuntimeError) as exc:
        await _runtime_with(client).verify_infrastructure()
    msg = str(exc.value)
    assert "127.0.0.1" in msg
    assert "172.28.100.99" in msg


async def test_gateway_on_wrong_network_fails(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    _patch_topology_config(monkeypatch)
    client = _FakeClient(
        _healthy_networks(),
        {GATEWAY: _gateway_info("127.0.0.1", network=EGRESS_NET)},
    )

    with pytest.raises(RuntimeError, match=AGENT_NET):
        await _runtime_with(client).verify_infrastructure()


# ---------------------------------------------------------------------------
# (d) gateway healthz
# ---------------------------------------------------------------------------


async def test_healthz_non_200_fails(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    async with _HealthzServer([503]) as http, _TcpServer() as tcp:
        _patch_topology_config(
            monkeypatch, healthz_port=http.port, nats_port=tcp.port
        )
        client = _FakeClient(
            _healthy_networks(), {GATEWAY: _gateway_info("127.0.0.1")}
        )

        with pytest.raises(RuntimeError) as exc:
            await _runtime_with(client).verify_infrastructure()
    assert "503" in str(exc.value)


async def test_healthz_connection_refused_fails_with_fix_hint(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    async with _TcpServer() as tcp:
        _patch_topology_config(
            monkeypatch, healthz_port=_free_port(), nats_port=tcp.port
        )
        client = _FakeClient(
            _healthy_networks(), {GATEWAY: _gateway_info("127.0.0.1")}
        )

        with pytest.raises(RuntimeError) as exc:
            await _runtime_with(client).verify_infrastructure()
    assert "healthz" in str(exc.value)
    assert _FIX_HINT in str(exc.value)


# ---------------------------------------------------------------------------
# (e) NATS reachability
# ---------------------------------------------------------------------------


async def test_nats_unreachable_fails_with_fix_hint(
    monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    async with _HealthzServer([200]) as http:
        _patch_topology_config(
            monkeypatch, healthz_port=http.port, nats_port=_free_port()
        )
        client = _FakeClient(
            _healthy_networks(), {GATEWAY: _gateway_info("127.0.0.1")}
        )

        with pytest.raises(RuntimeError) as exc:
            await _runtime_with(client).verify_infrastructure()
    assert "NATS" in str(exc.value)
    assert _FIX_HINT in str(exc.value)
