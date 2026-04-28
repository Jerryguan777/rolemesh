"""Idempotent EC-2 egress bootstrap shared by every code path that
spins up a ``ContainerRuntime`` and intends to spawn agent containers.

The orchestrator daemon (``rolemesh.main``) has historically been the
only caller, so the gateway-launch + DNS-IP-register logic lived
inline in ``main.py:_launch_egress_gateway_once_ready``. Other entry
points that started their own runtime (the eval CLI, future admin
scripts) silently inherited the runner's ``_EGRESS_GATEWAY_DNS_IP``
unset state and emitted "DNS exfil protection is inactive" warnings
on every container spawn, with no way for the caller to know they
needed to call the setter.

This module turns the missing step into a function call. The flow
is now:

* ``runtime.ensure_available()``  — dockerd version gate (caller)
* ``runtime.ensure_agent_network(...)`` — agent bridge (caller)
* ``runtime.ensure_egress_network(...)`` — outbound bridge (caller)
* ``await ensure_gateway_running_and_register_dns(runtime)`` — this
  module: idempotent gateway launch + DNS resolver registration.

The first three are already idempotent methods on the runtime itself.
The fourth was the only piece without a uniform entry point; this
module adds it.

Idempotence model
-----------------

* If the gateway container is already running and reachable, the
  helper does NOT recreate it (which would tear down a working
  gateway in use by other processes — e.g. the orchestrator daemon).
  It just inspects the existing container's IP and registers it.
* If the gateway is not running (fresh boot, or a prior crash left
  no container behind), the helper calls ``launch_egress_gateway``
  and ``wait_for_gateway_ready`` exactly as ``main.py`` did.

Cross-process race: two processes calling this helper simultaneously
on a host without a gateway can both try to launch one. Docker's
``create_or_replace`` semantics tolerate the conflict (last writer
wins), and ``wait_for_gateway_ready`` polls until /healthz responds,
so the race is recoverable. In practice the orchestrator launches
the gateway long before any second caller exists.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import aiodocker

from rolemesh.core.config import (
    CONTAINER_EGRESS_NETWORK_NAME,
    CONTAINER_NETWORK_NAME,
    CREDENTIAL_PROXY_PORT,
    EGRESS_GATEWAY_CONTAINER_NAME,
)
from rolemesh.core.logger import get_logger
from rolemesh.egress.launcher import launch_egress_gateway, wait_for_gateway_ready

# ``rolemesh.container.runner`` imports from ``rolemesh.agent`` which
# imports back into ``rolemesh.container.runner`` — module-level
# imports here would race that cycle on first load. The setter is
# pulled in inside the function body, exactly as ``main.py`` did
# before this refactor extracted the bootstrap.

if TYPE_CHECKING:
    from rolemesh.container.runtime import ContainerRuntime


logger = get_logger()


def _gateway_agent_net_ip(info: dict[str, object]) -> str | None:
    """Pull the gateway's agent-network IP from a docker inspect dict."""
    networks_obj = info.get("NetworkSettings")
    if not isinstance(networks_obj, dict):
        return None
    networks = networks_obj.get("Networks")
    if not isinstance(networks, dict):
        return None
    net_info = networks.get(CONTAINER_NETWORK_NAME)
    if not isinstance(net_info, dict):
        return None
    ip = net_info.get("IPAddress")
    return ip if isinstance(ip, str) and ip else None


async def _existing_gateway_ip(docker_client: aiodocker.Docker) -> str | None:
    """If a running gateway container exists, return its agent-net IP.

    Returns None when:
      * the container does not exist,
      * inspect raises (transient docker error),
      * the container exists but is not in ``Running`` state,
      * the inspect output lacks an agent-network IPAddress entry.

    All non-running / unreachable cases drop through to a fresh
    launch, which is the conservative behaviour: better to recreate
    a broken gateway than to register the IP of a stopped one.
    """
    try:
        container = docker_client.containers.container(EGRESS_GATEWAY_CONTAINER_NAME)
        info = await container.show()
    except aiodocker.exceptions.DockerError:
        return None
    state = info.get("State")
    if not isinstance(state, dict) or not state.get("Running"):
        return None
    return _gateway_agent_net_ip(info)


async def _registered_gateway_ip_after_launch(
    docker_client: aiodocker.Docker,
) -> str | None:
    """Inspect the freshly-launched gateway and return its agent-net IP.

    Separated from ``_existing_gateway_ip`` because the post-launch
    expectations are different — here the container is known to
    exist and an absent ``IPAddress`` is an unrecoverable error
    rather than a "fall through to launch" signal.
    """
    container = docker_client.containers.container(EGRESS_GATEWAY_CONTAINER_NAME)
    info = await container.show()
    return _gateway_agent_net_ip(info)


def _ec2_active(runtime: ContainerRuntime) -> bool:
    """EC-2 is active when both the operator opted in
    (``CONTAINER_NETWORK_NAME`` is non-empty) and the runtime
    backend supports egress networks (Docker today; k8s grows its
    own bootstrap path later).
    """
    return bool(
        CONTAINER_NETWORK_NAME
        and hasattr(runtime, "ensure_egress_network")
        and hasattr(runtime, "verify_egress_gateway_reachable")
    )


async def ensure_gateway_running_and_register_dns(
    runtime: ContainerRuntime,
) -> str | None:
    """Make sure the egress gateway is running and register its DNS
    IP for this process's container spawns.

    Returns the agent-network IP that was registered, or None when
    EC-2 is not active (returns silently — the caller is operating
    in rollback mode and there is nothing to register).

    Safe to call from any entry point that spins up a ``ContainerRuntime``
    independently of the orchestrator daemon — e.g. ``rolemesh-eval``,
    ad-hoc admin scripts, CI workers. Reuses an already-running
    gateway rather than recreating it, so concurrent callers (the
    orchestrator daemon plus an eval CLI invoked alongside it) do not
    disrupt each other.
    """
    if not _ec2_active(runtime):
        return None

    # Lazy import to break the runner ↔ agent.executor circular
    # dependency at module load time. Safe at call time.
    from rolemesh.container.runner import set_egress_gateway_dns_ip

    docker_client = runtime._ensure_client()  # type: ignore[attr-defined]

    # Reuse-if-running fast path. Skips ``launch_egress_gateway``'s
    # destructive ``create_or_replace`` so a running gateway in use
    # by other processes (orchestrator + eval CLI on the same host)
    # is left alone.
    existing_ip = await _existing_gateway_ip(docker_client)
    if existing_ip is not None:
        # Still verify reachability — a "Running" container with no
        # responder behind /healthz would silently break agent egress
        # otherwise. ``wait_for_gateway_ready`` polls for ~60s with
        # short timeouts; the no-op case (gateway healthy) returns
        # immediately on the first request.
        with contextlib.suppress(Exception):
            await wait_for_gateway_ready(
                docker_client,
                agent_network=CONTAINER_NETWORK_NAME,
                gateway_service_name=EGRESS_GATEWAY_CONTAINER_NAME,
                reverse_proxy_port=CREDENTIAL_PROXY_PORT,
            )
        set_egress_gateway_dns_ip(existing_ip)
        logger.info(
            "Egress gateway already running — registered DNS IP",
            ip=existing_ip,
            gateway=EGRESS_GATEWAY_CONTAINER_NAME,
        )
        return existing_ip

    # No usable gateway — launch one and wait for it.
    await launch_egress_gateway(
        docker_client,
        agent_network=CONTAINER_NETWORK_NAME,
        egress_network=CONTAINER_EGRESS_NETWORK_NAME,
    )
    await wait_for_gateway_ready(
        docker_client,
        agent_network=CONTAINER_NETWORK_NAME,
        gateway_service_name=EGRESS_GATEWAY_CONTAINER_NAME,
        reverse_proxy_port=CREDENTIAL_PROXY_PORT,
    )

    fresh_ip = await _registered_gateway_ip_after_launch(docker_client)
    if fresh_ip is None:
        # The healthy /healthz reachable just succeeded but inspect
        # has no IPAddress on agent-net — shouldn't happen with our
        # network topology. Log error rather than raise: agents will
        # fall back to Docker DNS, which is "broken" but not "down".
        logger.error(
            "Gateway healthy but its agent-net IP is missing from "
            "inspect output — agents will fall back to Docker DNS "
            "and the authoritative resolver will not see their queries",
            gateway=EGRESS_GATEWAY_CONTAINER_NAME,
            agent_network=CONTAINER_NETWORK_NAME,
        )
        return None

    set_egress_gateway_dns_ip(fresh_ip)
    logger.info(
        "Egress gateway launched — registered DNS IP",
        ip=fresh_ip,
        gateway=EGRESS_GATEWAY_CONTAINER_NAME,
    )
    return fresh_ip
