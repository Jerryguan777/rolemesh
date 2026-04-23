"""Session-scoped topology for egress integration tests.

The fixture graph:

    docker_client  (session)
         │
         ▼
    topology       (session)
         │
         ▼  Topology handle
    each test     (function)

Everything lives on per-run networks (``rolemesh-test-agent-{hex}``,
``rolemesh-test-egress-{hex}``) so CI parallelism doesn't collide and
a crash leaves stale topology you can inspect rather than corrupting
the production ``rolemesh-agent-net``.

These tests require Linux-native Docker. They are skipped under the
``@pytest.mark.integration`` marker unless ``-m integration`` (or
``-m ""`` to include everything) is passed.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import aiodocker
import aiodocker.exceptions
import pytest
import pytest_asyncio

from .helpers import (
    FAKE_UPSTREAM_IMAGE,
    GATEWAY_IMAGE,
    NATS_IMAGE,
    PROBE_IMAGE,
    ContainerHandle,
    Topology,
    ensure_image_pulled,
    rand_suffix,
    wait_for_http_ok,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def docker_client() -> AsyncIterator[aiodocker.Docker]:
    client = aiodocker.Docker()
    try:
        # Fail fast + with a clear message if dockerd isn't reachable
        # (e.g. running tests on a dev laptop without Docker up).
        await client.system.info()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker not available for integration tests: {exc}")
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def topology(docker_client: aiodocker.Docker) -> AsyncIterator[Topology]:
    """Build + start the full test topology once per session.

    Session scope is deliberate — the gateway image build + container
    cold starts are ~15-20s end-to-end. Doing this per-test would
    multiply that cost and overwhelm the suite.
    """
    # Pull / verify required images.
    for image in (PROBE_IMAGE, GATEWAY_IMAGE, NATS_IMAGE, FAKE_UPSTREAM_IMAGE):
        await ensure_image_pulled(docker_client, image)

    suffix = rand_suffix()
    agent_network = f"rolemesh-test-agent-{suffix}"
    egress_network = f"rolemesh-test-egress-{suffix}"

    # --- Networks ------------------------------------------------------
    # agent-net MUST be Internal=true to exercise the real block path.
    #
    # KNOWN DESIGN GAP (flagged by these integration tests): production
    # ``rolemesh-agent-net`` also sets ``enable_icc=false``, which
    # Docker enforces via iptables DROP rules between any two
    # containers on the bridge. That breaks the EC-1 assumption that
    # agents reach the gateway by service name — the FORWARD drop
    # applies to that traffic too. For the tests we relax ICC so the
    # gateway↔NATS and agent↔gateway paths actually work; the
    # upstream production config needs a matching fix (followup).
    await docker_client.networks.create(
        config={
            "Name": agent_network,
            "Driver": "bridge",
            "Internal": True,
            "Options": {"com.docker.network.bridge.enable_icc": "true"},
            "Labels": {"io.rolemesh.owner": "integration-test"},
        }
    )
    await docker_client.networks.create(
        config={
            "Name": egress_network,
            "Driver": "bridge",
            "Internal": False,
            "Options": {"com.docker.network.bridge.enable_icc": "true"},
            "Labels": {"io.rolemesh.owner": "integration-test"},
        }
    )

    # --- NATS ---------------------------------------------------------
    # Two-network attach: egress-net first (for host-port publishing —
    # Docker refuses PortBindings on Internal=true networks), agent-net
    # second (so the gateway can reach NATS by service name on the
    # bridge it shares with agents).
    nats_name = f"rolemesh-test-nats-{suffix}"
    nats_container = await docker_client.containers.create_or_replace(
        name=nats_name,
        config={
            "Image": NATS_IMAGE,
            "Cmd": ["-js"],
            "ExposedPorts": {"4222/tcp": {}},
            "HostConfig": {
                "NetworkMode": egress_network,  # primary: non-internal
                "AutoRemove": False,
                "PortBindings": {
                    "4222/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}],
                },
            },
        },
    )
    await nats_container.start()
    nats_handle = ContainerHandle(name=nats_name, docker=docker_client)

    # Secondary attach: agent-net, so gateway can reach ``nats`` by
    # service name. Alias matches the name the gateway's NATS_URL uses.
    agent_net_obj_for_nats = await docker_client.networks.get(agent_network)
    await agent_net_obj_for_nats.connect({"Container": nats_container._id})

    # Discover the randomly-assigned host port for NATS.
    nats_info = await nats_container.show()
    nats_host_port = int(
        nats_info["NetworkSettings"]["Ports"]["4222/tcp"][0]["HostPort"]
    )

    # Two NATS URLs: one for use inside Docker (gateway reaches by
    # service name), one for test code on the host (loopback + mapped
    # port). Must pass the correct one to each caller.
    nats_url_internal = f"nats://{nats_name}:4222"
    nats_url_host = f"nats://127.0.0.1:{nats_host_port}"

    # --- Fake upstream ----------------------------------------------
    # Hosted on egress-net with a known service name so the gateway's
    # CONNECT upstream reach works via Docker embedded DNS on egress-net.
    egress_net_obj = await docker_client.networks.get(egress_network)
    fake_upstream_name = f"fake-upstream-{suffix}"
    fake_container = await docker_client.containers.create_or_replace(
        name=fake_upstream_name,
        config={
            "Image": FAKE_UPSTREAM_IMAGE,  # prebuilt, aiohttp baked in
            "HostConfig": {
                "NetworkMode": egress_network,
                "AutoRemove": False,
                # Root inside a throwaway container — needed to bind :443
                # without CAP_NET_BIND_SERVICE.
            },
            "User": "0:0",
        },
    )
    # Start first so the container has an active endpoint before we
    # dual-attach. Docker allows the secondary network connect on a
    # created (not-yet-started) container, but the endpoint isn't
    # fully plumbed until start — attaching pre-start has tripped us
    # before.
    await fake_container.start()
    fake_handle = ContainerHandle(name=fake_upstream_name, docker=docker_client)

    # --- Wait for NATS to be ready ----------------------------------
    # The gateway will crash on boot if NATS isn't accepting connections
    # yet. Probe from the host via the mapped port.
    import nats  # type: ignore[import-not-found]

    for _ in range(30):
        try:
            nc_probe = await nats.connect(nats_url_host, connect_timeout=1)
            await nc_probe.close()
            break
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"NATS at {nats_url_host} never became ready")

    # Seed a rules responder BEFORE starting the gateway so the
    # gateway's fetch_snapshot_via_nats call succeeds at boot.
    bootstrap_nc = await nats.connect(nats_url_host)

    async def _empty_responder(msg: Any) -> None:
        with contextlib.suppress(Exception):
            await msg.respond(b'{"rules": []}')

    await bootstrap_nc.subscribe("egress.rules.snapshot.request", cb=_empty_responder)
    await bootstrap_nc.flush()

    # --- Gateway ------------------------------------------------------
    # Attach to both networks at create time with the ``egress-gateway``
    # alias on agent-net so probes can reach it by the production name.
    gateway_name = f"egress-gateway-{suffix}"
    gateway_container = await docker_client.containers.create_or_replace(
        name=gateway_name,
        config={
            "Image": GATEWAY_IMAGE,
            "Env": [
                f"NATS_URL={nats_url_internal}",
                # Point DNS upstream at Google/Cloudflare even in tests — the
                # gateway's DNS allow-path needs real recursion for one
                # smoke test. Block-path tests never reach upstream so
                # this isn't load-bearing for those.
                "EGRESS_UPSTREAM_DNS=8.8.8.8,1.1.1.1",
            ],
            "HostConfig": {
                "NetworkMode": agent_network,
                "AutoRemove": False,
                "RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3},
                "CapAdd": ["NET_BIND_SERVICE"],
            },
            "NetworkingConfig": {
                "EndpointsConfig": {
                    agent_network: {
                        "Aliases": ["egress-gateway"],
                    },
                },
            },
        },
    )
    # Attach to egress-net as a second interface so the gateway has a
    # real default route for upstream DNS + HTTP.
    await egress_net_obj.connect({"Container": gateway_container._id})
    await gateway_container.start()
    gateway_handle = ContainerHandle(name=gateway_name, docker=docker_client)

    # Wait for /healthz. Poll with extra attempts because the gateway
    # container's NATS connect + snapshot fetch adds 2-4s to cold start.
    await wait_for_http_ok(
        docker_client,
        network=agent_network,
        url="http://egress-gateway:3001/healthz",
        attempts=40,
    )

    # Resolve the gateway's IP on the agent network so probes can pin
    # it as their DNS resolver (for the DNS authoritative test path).
    gateway_info = await gateway_container.show()
    gateway_ip_on_agent = (
        gateway_info["NetworkSettings"]["Networks"][agent_network]["IPAddress"]
    )

    topo = Topology(
        docker=docker_client,
        agent_network=agent_network,
        egress_network=egress_network,
        gateway=gateway_handle,
        nats=nats_handle,
        fake_upstream=fake_handle,
        nats_url=nats_url_host,  # host-side test code uses the mapped port
        gateway_ip_on_agent_net=gateway_ip_on_agent,
        fake_upstream_name=fake_upstream_name,
    )

    try:
        yield topo
    finally:
        with contextlib.suppress(Exception):
            await bootstrap_nc.close()
        await topo.cleanup()


@pytest_asyncio.fixture(loop_scope="session")
async def per_test_cleanup(topology: Topology) -> AsyncIterator[None]:
    """Wipe per-test probe containers after each test.

    Session-scoped topology persists across tests, but probes spawned
    via ``topology.spawn_probe`` accumulate in ``containers_to_cleanup``
    — this fixture drains that list at function teardown so the next
    test starts with a clean container inventory.
    """
    yield
    # Copy then clear so cleanup is idempotent against double-teardown.
    handles = list(topology.containers_to_cleanup)
    topology.containers_to_cleanup.clear()
    for handle in handles:
        with contextlib.suppress(Exception):
            await handle.delete()
