"""Custom Docker bridge network for agent containers (R5, R5.1).

Owns two concerns:

1. Idempotent creation of the RoleMesh agent bridge. The network has
   inter-container communication disabled (enable_icc=false) so that
   compromised agent containers cannot pivot to other agents in the
   same tenant or across tenants.

2. Startup-time connectivity self-check. After the credential proxy
   has started, a throwaway probe container is attached to the network
   and attempts an HTTP GET against host.docker.internal:<PROXY_PORT>/health.
   Failure here means agents will silently lose all external MCP /
   credentialed traffic — we make the orchestrator refuse to enter
   ready state instead.

The long-term improvement (tracked as a TODO in docs) is to move the
credential proxy itself onto the rolemesh-agent-net so that agents reach
it through a service name instead of the host-gateway escape hatch; the
current ExtraHosts approach keeps the smaller change surface for this
hardening pass.
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
    """Create the agent bridge if missing; verify icc=false if it already exists.

    Idempotent by name. If the network exists with the wrong ICC setting
    this logs a warning rather than deleting/recreating — reusing an
    existing network avoids surprise downtime for running containers,
    and the caller can manually remove the network to trigger a clean
    recreate.
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
        if icc != "false":
            logger.warning(
                "Agent network exists with ICC enabled — containers on "
                "this bridge can talk to each other; recreate the network "
                "to enforce isolation",
                network=network_name,
                options=opts,
            )
        else:
            logger.info("Reusing existing agent network", network=network_name)
        return

    config: dict[str, Any] = {
        "Name": network_name,
        "Driver": "bridge",
        "Options": _NETWORK_OPTIONS,
        # Labels let operators see this network was created by RoleMesh
        # (so `docker network prune` / audit scripts don't mistake it for
        # an abandoned user network).
        "Labels": {"io.rolemesh.owner": "orchestrator"},
    }
    await client.networks.create(config=config)
    logger.info("Created agent network", network=network_name, options=_NETWORK_OPTIONS)


async def verify_proxy_reachable(
    client: aiodocker.Docker,
    network_name: str,
    proxy_port: int,
    *,
    timeout_s: float = 10.0,
) -> None:
    """Prove that containers on the agent network can reach the credential
    proxy over the host-gateway path (R5.1-2).

    Raises RuntimeError on failure — this is a fail-closed gate, we refuse
    to enter ready state when the path agents actually use is broken.

    What we test: the probe container can reach the proxy's TCP port AND
    the listener speaks HTTP. What we do NOT test: that the proxy's
    forwarding logic is correct, nor that any particular endpoint exists.
    A real agent request (e.g. POST /v1/messages) is proxied to Anthropic,
    and Anthropic's response status may be anything (200, 401, 404, ...).
    So the probe criterion is "ANY well-formed HTTP status line comes
    back", which proves:
        1. rolemesh-agent-net → host-gateway routing is up (R5.1-2)
        2. dockerd accepted our HostConfig.ExtraHosts host-gateway magic
        3. A process on the host port is speaking HTTP (not a stale TCP
           listener from some other service)
    That's the exact surface whose silent failure would produce the
    "proxy is there, agents can't reach it" failure mode we're guarding.

    An earlier revision probed a hardcoded /health path and required a
    200. That was a category error: the credential proxy's catch-all
    route forwards unknown paths upstream to Anthropic, so /health
    returned a 404 from Anthropic — proof that connectivity works, but
    the probe interpreted it as failure and blocked every startup.
    This docstring + probe command together pin the lesson.

    On Linux host-gateway requires dockerd >= 20.10. DockerRuntime._check_daemon_version
    gates that separately; here we assume the version check has already passed.
    """
    if not network_name:
        logger.info("Skipping proxy reachability probe — no custom network configured")
        return

    # uuid suffix so concurrent orchestrators (HA deploys, parallel
    # integration tests) don't race on container create/delete. The
    # finally-block cleanup uses this specific name, so there's no
    # leaked-container risk from the randomization.
    probe_name = f"rolemesh-proxy-probe-{uuid.uuid4().hex[:8]}"
    # -S prints the server response line ("HTTP/1.1 <code> <reason>") to
    # stderr for every HTTP code, so grep matches whether the proxy
    # returned 200, 401, 404, or 5xx. Only connection refused / timeout
    # / non-HTTP responses cause the probe to fail.
    # -O /dev/null discards the body; -t 1 keeps retries off.
    #
    # wget's internal timeout is derived from the caller's timeout_s and
    # capped at 2s below it, so the outer asyncio.wait_for window always
    # gets a chance to observe wget's exit cleanly rather than racing it.
    wget_timeout = max(1, int(timeout_s) - 2)
    probe_cmd = [
        "sh",
        "-c",
        f"wget -S -O /dev/null -T {wget_timeout} -t 1 "
        f"http://host.docker.internal:{proxy_port}/ 2>&1 | grep -q 'HTTP/'",
    ]

    config: dict[str, Any] = {
        "Image": _PROBE_IMAGE,
        "Cmd": probe_cmd,
        "HostConfig": {
            "NetworkMode": network_name,
            "AutoRemove": False,  # we delete explicitly — AutoRemove races with wait()
            "ExtraHosts": ["host.docker.internal:host-gateway"],
        },
        "AttachStdout": True,
        "AttachStderr": True,
    }

    # Pull the probe image if absent. Small-image path; ignore errors and
    # let the create() call below surface the real problem.
    try:
        await client.images.inspect(_PROBE_IMAGE)
    except aiodocker.exceptions.DockerError:
        logger.info("Pulling probe image", image=_PROBE_IMAGE)
        try:
            await client.images.pull(_PROBE_IMAGE)
        except aiodocker.exceptions.DockerError as exc:
            logger.warning(
                "Could not pull probe image — skipping connectivity check; "
                "real agents may still fail to reach the credential proxy",
                image=_PROBE_IMAGE,
                error=str(exc),
            )
            return

    # Wipe a stale probe from a prior crashed orchestrator run.
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
                f"Credential-proxy connectivity probe timed out after "
                f"{timeout_s}s (network={network_name}, port={proxy_port}). "
                "Agents on this network will not reach the proxy — refusing "
                "to continue. Check host-gateway support and firewall rules."
            )
            raise RuntimeError(msg) from exc
        exit_code = int(result.get("StatusCode", -1))
        if exit_code != 0:
            logs = await container.log(stdout=True, stderr=True)
            msg = (
                f"Credential-proxy connectivity probe failed "
                f"(exit={exit_code}, network={network_name}, "
                f"port={proxy_port}). Agents will not reach the proxy. "
                f"Probe output: {''.join(logs)[:500]}"
            )
            raise RuntimeError(msg)
        logger.info("Credential-proxy reachable from agent network", network=network_name)
    finally:
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await container.delete(force=True)
