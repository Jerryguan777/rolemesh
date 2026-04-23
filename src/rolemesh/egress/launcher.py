"""Orchestrator-side: launch and wait-for-ready the egress gateway container.

Runs at orchestrator startup. The gateway must be up before any agent
container is scheduled — without it, agents attached to the
``Internal=true`` agent bridge have no route to the internet.

Contract:
    launch_egress_gateway()     — idempotent; ensures a gateway
                                  container named EGRESS_GATEWAY_CONTAINER_NAME
                                  is running and attached to both the
                                  agent and egress bridges. Removes any
                                  stale instance from a prior run first.
    wait_for_gateway_ready()    — poll-based probe; retries the
                                  connectivity check until it succeeds
                                  or the budget elapses. Caller should
                                  fail-close on exception.

Why two steps: container "running" from dockerd's perspective just means
the ENTRYPOINT process has not exited, not that its HTTP listener is
bound. Without the second step, ``verify_egress_gateway_reachable`` is
racy on a cold-start orchestrator.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import aiodocker
import aiodocker.exceptions

from rolemesh.container.network import verify_egress_gateway_reachable
from rolemesh.core.config import (
    CREDENTIAL_PROXY_PORT,
    EGRESS_GATEWAY_CONTAINER_NAME,
    EGRESS_GATEWAY_IMAGE,
    PROJECT_ROOT,
)
from rolemesh.core.logger import get_logger

logger = get_logger()


async def launch_egress_gateway(
    client: aiodocker.Docker,
    *,
    agent_network: str,
    egress_network: str,
    image: str = EGRESS_GATEWAY_IMAGE,
    container_name: str = EGRESS_GATEWAY_CONTAINER_NAME,
) -> None:
    """Start (or replace) the gateway container on both bridges."""

    # Fail fast with a clear message if the image hasn't been built.
    # Happens BEFORE touching any container state so an image-missing
    # orchestrator doesn't flap stale containers around pointlessly.
    try:
        await client.images.inspect(image)
    except aiodocker.exceptions.DockerError as exc:
        msg = (
            f"Egress gateway image not found: {image!r}. "
            "Build it first: `docker build -f container/Dockerfile.egress-gateway "
            "-t rolemesh-egress-gateway:latest .`"
        )
        raise RuntimeError(msg) from exc

    # Remove stale gateway from a prior orchestrator run. force=True also
    # stops if running; safe because the gateway is stateless in EC-1.
    with contextlib.suppress(aiodocker.exceptions.DockerError):
        stale = client.containers.container(container_name)
        await stale.delete(force=True)
        logger.info("Removed stale egress gateway", name=container_name)

    # Primary network is agent-net. Docker embedded DNS binds the
    # container name to its bridge IP for each network it attaches to;
    # agent bridge siblings resolve ``egress-gateway`` → the gateway's
    # agent-net IP automatically.
    host_config: dict[str, Any] = {
        "NetworkMode": agent_network,
        "RestartPolicy": {"Name": "unless-stopped"},
        # Bind-mount the host .env file so the existing credential-
        # injection logic continues to work inside the container. The
        # gateway's WORKDIR is /app, which is where ``read_env_file``
        # looks by default (Path.cwd() / ".env"). Read-only to avoid
        # surprise writes.
        "Binds": _optional_env_bind(),
    }

    config: dict[str, Any] = {
        "Image": image,
        "HostConfig": host_config,
        "Labels": {"io.rolemesh.owner": "orchestrator", "io.rolemesh.role": "egress-gateway"},
        "AttachStdout": False,
        "AttachStderr": False,
    }

    container = await client.containers.create_or_replace(name=container_name, config=config)

    # Attach the second network BEFORE start so the gateway sees both
    # interfaces from its first moment of existence. Connecting post-
    # start creates a window where the gateway thinks it has no egress
    # route and may attempt a fallback that then doesn't match steady
    # state.
    try:
        egress = await client.networks.get(egress_network)
        await egress.connect({"Container": container._id})
    except aiodocker.exceptions.DockerError:
        # Best-effort cleanup — if the egress network attach fails the
        # gateway can't do its job, so roll back the container create.
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await container.delete(force=True)
        raise

    await container.start()
    logger.info(
        "Egress gateway launched",
        name=container_name,
        image=image,
        agent_network=agent_network,
        egress_network=egress_network,
    )


async def wait_for_gateway_ready(
    client: aiodocker.Docker,
    *,
    agent_network: str,
    gateway_service_name: str = EGRESS_GATEWAY_CONTAINER_NAME,
    reverse_proxy_port: int = CREDENTIAL_PROXY_PORT,
    attempts: int = 15,
    interval_s: float = 1.0,
) -> None:
    """Probe /healthz with retry until the gateway is serving or budget exhausts.

    The first few attempts typically fail with a non-zero exit code
    because the aiohttp listener isn't bound yet. A budget around
    ``attempts * interval_s`` seconds gives a cold-start Python container
    room to finish its imports on slow hosts.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            await verify_egress_gateway_reachable(
                client,
                network_name=agent_network,
                gateway_service_name=gateway_service_name,
                reverse_proxy_port=reverse_proxy_port,
            )
            return
        except RuntimeError as exc:
            last_exc = exc
            logger.debug(
                "Gateway not ready yet, retrying",
                attempt=attempt,
                max_attempts=attempts,
            )
            await asyncio.sleep(interval_s)
    assert last_exc is not None
    msg = (
        f"Egress gateway did not become ready after {attempts} attempts "
        f"({attempts * interval_s:.1f}s). Last error: {last_exc}"
    )
    raise RuntimeError(msg) from last_exc


def _optional_env_bind() -> list[str]:
    """Bind-mount the host .env into the gateway container if present.

    Returns an empty list when no .env exists (typical for test /
    containerized-deploy environments that inject secrets via docker
    --env-file). The credential proxy then relies on os.environ and
    gracefully degrades when a provider's key is absent.
    """
    env_path = Path(PROJECT_ROOT) / ".env"
    if env_path.is_file():
        return [f"{env_path}:/app/.env:ro"]
    logger.info(
        "No host .env found — gateway container will read secrets from its process env only",
        project_root=str(PROJECT_ROOT),
    )
    return []
