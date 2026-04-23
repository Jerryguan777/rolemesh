"""Custom Docker bridge networks for agent + egress gateway (R5, R5.1, EC-1).

Owns three concerns:

1. Idempotent creation of the RoleMesh agent bridge. ICC is disabled
   (``enable_icc=false``) so compromised agent containers cannot pivot
   to other agents on the same bridge. As of EC-1 the bridge is also
   created with ``Internal=true`` — Docker does not install an outbound
   route, so an agent that bypasses the orchestrator-injected proxy env
   still hits EHOSTUNREACH rather than the open internet.

2. Idempotent creation of the egress bridge. This is a plain bridge
   (not internal) on which only the egress gateway container sits, so
   the gateway gets a real default route while staying isolated from
   other workloads through ICC disabled.

3. Startup-time connectivity self-check against the egress gateway.
   After the gateway is launched, a throwaway probe container attached
   to the internal agent bridge confirms it can reach
   ``http://egress-gateway:<PORT>/healthz`` by service name. Failure
   here means agents would silently lose all outbound traffic — the
   orchestrator refuses to enter ready state instead.

Pre-EC-1 versions of this file probed the credential proxy via
``host.docker.internal:<PROXY_PORT>/`` and accepted any HTTP status line
as proof of connectivity. EC-1 replaces that with a structured
``/healthz`` probe against the gateway service name; the old path is
removed, not wrapped, because with ``Internal=true`` the
host-gateway path no longer resolves.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import aiodocker
import aiodocker.exceptions

from rolemesh.core.logger import get_logger

logger = get_logger()


# IPC disabled → compromised containers on the same bridge cannot reach
# each other. This is the whole point of leaving Docker's default bridge.
_NETWORK_OPTIONS: dict[str, str] = {"com.docker.network.bridge.enable_icc": "false"}

# Small busybox-like image used only for the connectivity probe. Kept
# intentionally tiny; alpine is already present in most dev environments
# and pulls in under ~5MB.
_PROBE_IMAGE: str = "alpine:3.19"


async def ensure_agent_network(
    client: aiodocker.Docker,
    network_name: str,
) -> None:
    """Create the agent bridge with ``Internal=true`` if missing.

    Idempotent by name. If the network already exists and is NOT
    internal (pre-EC-1 state), we log a warning and reuse it rather
    than forcibly recreate. Recreating would disconnect every running
    agent container; the operator can stop orchestrator traffic,
    ``docker network rm rolemesh-agent-net`` manually, and restart to
    pick up the new Internal flag.

    The same warning fires when ICC is unexpectedly enabled on a
    re-used network — both invariants (Internal=true, ICC=false) are
    load-bearing for agent isolation.
    """
    if not network_name:
        logger.info("CONTAINER_NETWORK_NAME is empty — using Docker default bridge")
        return

    try:
        existing = await client.networks.get(network_name)
    except aiodocker.exceptions.DockerError as exc:
        if exc.status != 404:
            raise
        existing = None

    if existing is not None:
        info: dict[str, Any] = await existing.show()
        opts: dict[str, str] = info.get("Options", {}) or {}
        icc = opts.get("com.docker.network.bridge.enable_icc", "true").lower()
        internal = bool(info.get("Internal", False))
        problems: list[str] = []
        if icc != "false":
            problems.append("ICC enabled")
        if not internal:
            problems.append("not Internal")
        if problems:
            logger.warning(
                "Agent network exists with weakened isolation — recreate "
                "the network to enforce EC-1 invariants",
                network=network_name,
                problems=problems,
                options=opts,
            )
        else:
            logger.info("Reusing existing agent network", network=network_name)
        return

    config: dict[str, Any] = {
        "Name": network_name,
        "Driver": "bridge",
        # EC-1: Internal=true removes the bridge's default gateway. An
        # agent container on this network cannot reach any IP outside
        # the bridge without going through a dual-homed proxy (the
        # egress gateway). This is the physical anchor of the "no
        # bypass" property — cap drops alone can't guarantee no library
        # in the container's dependency tree shells out with a direct
        # socket.
        "Internal": True,
        "Options": _NETWORK_OPTIONS,
        # Labels let operators see this network was created by RoleMesh
        # (so `docker network prune` / audit scripts don't mistake it for
        # an abandoned user network).
        "Labels": {"io.rolemesh.owner": "orchestrator"},
    }
    await client.networks.create(config=config)
    logger.info(
        "Created agent network",
        network=network_name,
        options=_NETWORK_OPTIONS,
        internal=True,
    )


async def ensure_egress_network(
    client: aiodocker.Docker,
    network_name: str,
) -> None:
    """Create the egress bridge if missing.

    This bridge carries the egress gateway's outbound traffic. It is
    NOT internal — the gateway needs a real route out. Only the gateway
    container should attach to this bridge; ``enable_icc=false`` keeps
    any accidentally-attached neighbour from reaching the gateway
    directly (they'd have to route via the bridge gateway, which is a
    separate audit-able path).
    """
    if not network_name:
        logger.info(
            "CONTAINER_EGRESS_NETWORK_NAME is empty — skipping egress bridge "
            "creation; gateway launch will fail"
        )
        return

    try:
        existing = await client.networks.get(network_name)
    except aiodocker.exceptions.DockerError as exc:
        if exc.status != 404:
            raise
        existing = None

    if existing is not None:
        info: dict[str, Any] = await existing.show()
        opts: dict[str, str] = info.get("Options", {}) or {}
        icc = opts.get("com.docker.network.bridge.enable_icc", "true").lower()
        internal = bool(info.get("Internal", False))
        if internal:
            logger.warning(
                "Egress network exists but is marked Internal — the gateway "
                "won't be able to reach upstream; recreate the network",
                network=network_name,
            )
        elif icc != "false":
            logger.warning(
                "Egress network exists with ICC enabled — recreate to "
                "enforce gateway-only attachment",
                network=network_name,
                options=opts,
            )
        else:
            logger.info("Reusing existing egress network", network=network_name)
        return

    config: dict[str, Any] = {
        "Name": network_name,
        "Driver": "bridge",
        "Internal": False,
        "Options": _NETWORK_OPTIONS,
        "Labels": {"io.rolemesh.owner": "orchestrator"},
    }
    await client.networks.create(config=config)
    logger.info("Created egress network", network=network_name, options=_NETWORK_OPTIONS)


async def verify_egress_gateway_reachable(
    client: aiodocker.Docker,
    network_name: str,
    gateway_service_name: str,
    reverse_proxy_port: int,
    *,
    timeout_s: float = 10.0,
) -> None:
    """Prove that containers on the internal agent bridge can reach the
    egress gateway by service name (EC-1 R5).

    Raises RuntimeError on failure — this is a fail-closed gate; without
    a reachable gateway, every agent turn would time out on its first
    outbound request and manifest as a mysterious LLM-call failure many
    layers up. Much better to refuse startup.

    Probe semantics:
        * DNS: ``<gateway_service_name>`` must resolve on the internal
          bridge. Docker's embedded DNS binds the container name to its
          bridge IP; EC-1 agents won't have their DNS pointed at the
          gateway's resolver yet (EC-2 turns that on), so we still use
          127.0.0.11 here. Once EC-2 lands and the container DNS is
          pinned to the gateway, this same probe continues to work.
        * HTTP: ``GET /healthz`` must return 200. Unlike the pre-EC-1
          probe this path is our own — we expect a specific status, so
          the old "any HTTP response means success" logic doesn't apply.

    On failure the caller must treat orchestrator startup as aborted.
    """
    if not network_name:
        logger.info("Skipping gateway reachability probe — no custom network configured")
        return

    probe_name = f"rolemesh-egress-probe-{uuid.uuid4().hex[:8]}"
    wget_timeout = max(1, int(timeout_s) - 2)
    # Expect HTTP/200 specifically; /healthz is under our control so any
    # other code is a real problem.
    probe_cmd = [
        "sh",
        "-c",
        (
            f"wget -S -O /dev/null -T {wget_timeout} -t 1 "
            f"http://{gateway_service_name}:{reverse_proxy_port}/healthz "
            "2>&1 | grep -q 'HTTP/1.[01] 200'"
        ),
    ]

    config: dict[str, Any] = {
        "Image": _PROBE_IMAGE,
        "Cmd": probe_cmd,
        "HostConfig": {
            "NetworkMode": network_name,
            "AutoRemove": False,
            # No ExtraHosts: the gateway is resolved by service name via
            # Docker embedded DNS on the agent bridge. Keeping this
            # empty is a soft assertion that we no longer depend on the
            # host-gateway escape hatch.
        },
        "AttachStdout": True,
        "AttachStderr": True,
    }

    try:
        await client.images.inspect(_PROBE_IMAGE)
    except aiodocker.exceptions.DockerError:
        logger.info("Pulling probe image", image=_PROBE_IMAGE)
        try:
            await client.images.pull(_PROBE_IMAGE)
        except aiodocker.exceptions.DockerError as exc:
            logger.warning(
                "Could not pull probe image — skipping connectivity check; "
                "real agents may still fail to reach the egress gateway",
                image=_PROBE_IMAGE,
                error=str(exc),
            )
            return

    with contextlib.suppress(aiodocker.exceptions.DockerError):
        stale = client.containers.container(probe_name)
        await stale.delete(force=True)

    container = await client.containers.create_or_replace(name=probe_name, config=config)
    try:
        await container.start()
        try:
            result = await asyncio.wait_for(container.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            msg = (
                f"Egress-gateway connectivity probe timed out after "
                f"{timeout_s}s (network={network_name}, "
                f"service={gateway_service_name}:{reverse_proxy_port}). "
                "Agents on this network will not reach the gateway — "
                "refusing to continue. Is the gateway container running "
                "and attached to both networks?"
            )
            raise RuntimeError(msg) from exc
        exit_code = int(result.get("StatusCode", -1))
        if exit_code != 0:
            logs = await container.log(stdout=True, stderr=True)
            msg = (
                f"Egress-gateway connectivity probe failed "
                f"(exit={exit_code}, network={network_name}, "
                f"service={gateway_service_name}:{reverse_proxy_port}). "
                f"Probe output: {''.join(logs)[:500]}"
            )
            raise RuntimeError(msg)
        logger.info(
            "Egress gateway reachable from agent network",
            network=network_name,
            service=gateway_service_name,
        )
    finally:
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await container.delete(force=True)
