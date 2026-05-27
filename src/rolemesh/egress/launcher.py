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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiodocker
import aiodocker.exceptions

from rolemesh.container.network import verify_egress_gateway_reachable
from rolemesh.container.runtime import (
    get_host_gateway_extra_hosts,
    rewrite_loopback_to_host_gateway,
)
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
        # Bind-mount the host .env into /app/.env read-only so the
        # ``rolemesh.bootstrap`` auto-load inside the gateway
        # process picks up the same secrets the orchestrator uses.
        # (Bootstrap's resolver walks CWD; the image's WORKDIR is
        # /app.) Deployments using docker --env-file or K8s envFrom
        # instead of a host .env get an empty bind list and rely on
        # the env vars already present in the container environment.
        "Binds": _optional_env_bind(),
        # CAP_NET_BIND_SERVICE lets the non-root gateway user bind
        # UDP/53 for the authoritative DNS resolver. The image itself
        # drops every other privilege (no USER root, no other caps),
        # so this is the minimum grant needed for EC-2's DNS surface.
        # EC-1 did not need the cap because the gateway only bound
        # port 3001.
        "CapAdd": ["NET_BIND_SERVICE"],
        # On Linux, map host.docker.internal to the host so the gateway
        # can reach NATS / DB running on the host during local dev.
        # Docker Desktop already resolves this automatically; the helper
        # returns an empty dict there, so ExtraHosts stays unset.
        **(
            {"ExtraHosts": [f"{h}:{ip}" for h, ip in _extra_hosts().items()]}
            if _extra_hosts()
            else {}
        ),
    }

    config: dict[str, Any] = {
        "Image": image,
        "HostConfig": host_config,
        "Labels": {"io.rolemesh.owner": "orchestrator", "io.rolemesh.role": "egress-gateway"},
        "Env": _gateway_env(),
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

    Returns an empty list when no .env exists (typical for
    containerized deploys that inject secrets via docker --env-file
    or K8s secrets). In that case the gateway's ``_gateway_env``
    below forwards the provider-secret env vars explicitly, and
    ``rolemesh.bootstrap`` reads them from the process env on
    startup — same lookup path as a .env-backed deploy.
    """
    env_path = Path(PROJECT_ROOT) / ".env"
    if env_path.is_file():
        return [f"{env_path}:/app/.env:ro"]
    logger.info(
        "No host .env found — gateway will rely on provider-secret "
        "env vars forwarded via the container's Env block.",
        project_root=str(PROJECT_ROOT),
    )
    return []


def _extra_hosts() -> dict[str, str]:
    return get_host_gateway_extra_hosts()


# ---------------------------------------------------------------------------
# Forwardable env spec — single source of truth for "what env reaches
# the gateway container, and which ones are URLs that need loopback
# rewrite at the publish boundary"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ForwardSpec:
    """One env var to forward into the gateway container.

    ``key`` is the variable name. ``is_url`` flags values that need
    ``rewrite_loopback_to_host_gateway`` applied at the publish
    boundary (Bug 5 family — container-internal ``localhost`` is
    the container's loopback, not the host).

    ``requires`` makes the forward conditional on another host env
    var being set — used to avoid leaking generic AWS context (e.g.
    ``AWS_REGION`` set for ``aws cli`` use) into the gateway when
    Bedrock is not actually configured. Defaults to ``None``
    (unconditional).

    Token-shaped specs leave ``is_url=False`` and are forwarded
    verbatim. We deliberately do NOT run ``string.replace`` on
    every value: a secret that happens to contain ``://localhost:``
    bytes would otherwise be silently corrupted.
    """

    key: str
    is_url: bool = False
    requires: str | None = None


# Forward-only allowlist. LLM API keys are NOT on this list — credentials
# are read from the per-tenant ``tenant_model_credentials`` table via
# :class:`rolemesh.egress.credentials.CredentialResolver` inside the
# gateway. Only deployment-level *_BASE_URL overrides (non-secret) and
# infra knobs travel here.
#
# INV-CRED: anything matching ``*_API_KEY`` / ``*_AUTH_TOKEN`` /
# ``*_OAUTH_TOKEN`` / ``*BEARER*`` MUST NOT be added back. The lint
# script in PR 3 (``scripts/check_credential_routing.py``) fires on
# any ``os.environ.get`` of those shapes inside ``src/rolemesh/egress/``,
# but a regression here would not be caught — only an attentive review.
_FORWARDABLE: tuple[_ForwardSpec, ...] = (
    _ForwardSpec("EGRESS_UPSTREAM_DNS"),
    _ForwardSpec("ANTHROPIC_BASE_URL", is_url=True),
    _ForwardSpec("OPENAI_BASE_URL", is_url=True),
    _ForwardSpec("GOOGLE_BASE_URL", is_url=True),
    # Master key for the CredentialVault. Same value as the
    # orchestrator/webui processes — Fernet symmetric, so the
    # gateway needs it to decrypt rows the wizard wrote on the host.
    # NOT a per-tenant secret: classification "App master secret" per
    # docs/config-drift-fix-plan.md §2.1.
    _ForwardSpec("CREDENTIAL_VAULT_KEY"),
)


def _gateway_env() -> list[str]:
    """Build the Env block for the gateway container.

    The gateway reads NATS_URL for its NATS subscriptions and the
    deployment-level *_BASE_URL overrides forwarded here. LLM
    credentials are NOT forwarded — they come from the per-tenant
    ``tenant_model_credentials`` table via CredentialResolver inside
    the gateway, so the gateway container's env is intentionally
    secret-free for LLM providers.

    Forwarder spec lives in ``_FORWARDABLE`` (module-level) — single
    source of truth for both "which env vars cross the boundary" AND
    "which ones need loopback rewrite". See ``_ForwardSpec`` for the
    rationale.
    """
    import os as _os

    from rolemesh.core.config import NATS_URL as _NATS_URL

    env_pairs: list[str] = [f"NATS_URL={rewrite_loopback_to_host_gateway(_NATS_URL)}"]

    for spec in _FORWARDABLE:
        value = _os.environ.get(spec.key)
        if not value:
            continue
        if spec.requires and not _os.environ.get(spec.requires):
            continue
        if spec.is_url:
            value = rewrite_loopback_to_host_gateway(value)
        env_pairs.append(f"{spec.key}={value}")
    return env_pairs
