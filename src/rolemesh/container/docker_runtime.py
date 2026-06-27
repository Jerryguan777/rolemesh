"""Docker Engine API implementation using aiodocker.

Replaces all subprocess-based Docker calls with the Docker Engine API.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiodocker
import aiodocker.containers
import aiodocker.exceptions

from rolemesh.core.logger import get_logger


class IncompatibleDockerVersionError(RuntimeError):
    """Raised when the connected dockerd is older than the hardening floor.

    host-gateway in --add-host (used by RoleMesh to let agent containers
    reach the credential proxy through a custom bridge network) was
    introduced in Docker 20.10. Below that, the whole custom-network
    path silently breaks; we fail closed instead.
    """


_MIN_DOCKERD_VERSION: tuple[int, int] = (20, 10)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from rolemesh.container.runtime import ContainerSpec, VolumeMount

logger = get_logger()

# Memory suffix multipliers
_MEM_SUFFIXES: dict[str, int] = {
    "b": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
}


def _parse_memory(value: str) -> int:
    """Parse a memory string like '512m' into bytes."""
    value = value.strip().lower()
    for suffix, multiplier in _MEM_SUFFIXES.items():
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * multiplier
    return int(value)


_DOCKER_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)")


def _normalize_image_ref(ref: str) -> str:
    """Strip ambient Docker Hub prefixes so equivalent refs compare equal.

    Docker reports ``Image`` as written when the container was created.
    A user-side ``rolemesh-agent:latest`` and a registry-qualified
    ``docker.io/library/rolemesh-agent:latest`` point at the same image
    but compare ``!=`` as plain strings; normalize the well-known
    library form to the bare name so the whitelist works for both.
    """
    if not ref:
        return ""
    for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/library/", "index.docker.io/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _parse_docker_version(value: str) -> tuple[int, int] | None:
    """Parse a dockerd version string into (major, minor).

    Handles all the shapes dockerd emits in the wild:
      '24.0.7'       → (24, 0)
      '20.10.0'      → (20, 10)
      '20.10-rc1'    → (20, 10)    ← regression guard: old split()-based
                                       parser failed on this, causing the
                                       version-gate check to be silently
                                       skipped on RC builds.
      '28.2.2-ce'    → (28, 2)
      'canary-build' → None
      ''             → None
    """
    if not value:
        return None
    m = _DOCKER_VERSION_RE.match(value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _is_docker_socket_path(path: str) -> bool:
    """Return True iff *path* has basename "docker.sock".

    The earlier implementation used `"docker.sock" in path`, which
    produced false positives against legitimate paths like
    `/tmp/docker.socket-tests/foo` or `/home/agent/docker.socks.log`
    (substring collision with `.socket` / `.socks`). Basename match is
    the right granularity: Docker's actual control socket is always the
    file basename `docker.sock`; operators who need different names can
    rename the symlink on the host side, which this function would not
    reject (nor should it — that is a mount-allowlist decision, not a
    pattern-match decision).

    Symlink bypass on the host is a separate concern and is covered at
    the mount-allowlist layer (`rolemesh.security.mount_security`):
    only paths under the operator-configured roots may be bound, so an
    attacker-planted symlink outside those roots cannot reach this
    function in the first place.
    """
    return os.path.basename(path.rstrip("/\\")) == "docker.sock"


def _mounts_to_binds(mounts: list[VolumeMount]) -> list[str]:
    """Convert VolumeMount list to Docker bind strings.

    Enforces the docker-socket blockade (R6): no bind may expose
    docker.sock (on either host or container side) to an agent
    container — a socket bind would hand the agent root on the host.
    This is not expected to trigger in production (mount_security.py
    validates earlier), but is kept here as defence in depth: the final
    serialization is the last chance to catch a misconfiguration before
    the Docker API call.
    """
    for m in mounts:
        if _is_docker_socket_path(m.host_path) or _is_docker_socket_path(m.container_path):
            msg = (
                f"Refusing to mount docker socket into a container: "
                f"{m.host_path}:{m.container_path}"
            )
            raise ValueError(msg)
    return [f"{m.host_path}:{m.container_path}:{'ro' if m.readonly else 'rw'}" for m in mounts]


# ---------------------------------------------------------------------------
# DooD bind-source translation (docs/21 §7.1).
#
# When the orchestrator runs inside a container, the bind sources it
# assembles (DATA_DIR / "tenants/...") are paths in its own filesystem,
# but the host dockerd that creates the agent sandbox resolves bind
# sources against the HOST filesystem. ROLEMESH_HOST_DATA_DIR carries the
# host path that the deployment layer mounted onto DATA_DIR; every bind
# source under DATA_DIR is rewritten to ROLEMESH_HOST_DATA_DIR/<relpath>
# before it reaches the Docker API. Empty ROLEMESH_HOST_DATA_DIR (host
# dev flow, tests) disables translation entirely.
# ---------------------------------------------------------------------------


def _translate_bind_source(
    host_path: str,
    *,
    data_dir: str,
    host_data_dir: str,
) -> str:
    """Translate one orchestrator-visible bind source to a host path.

    Pure function: no filesystem access (the orchestrator container
    cannot stat host paths anyway). Paths are normalized lexically
    (``normpath``) before comparison so ``DATA_DIR/x/../../etc`` cannot
    masquerade as "under DATA_DIR" and escape via the translated root.

    Paths NOT under DATA_DIR (``additional_mounts`` like ``~/projects``)
    are passed through unchanged: dockerd interprets them against the
    host, so they keep working — but the orchestrator can no longer
    check their existence, and dockerd silently creates an empty
    root-owned directory for a missing bind source. The caller logs a
    prominent warning for this case (DooD semantics, docs/21 §7.1).
    """
    if not host_data_dir:
        return host_path
    normalized = Path(os.path.normpath(host_path))
    data_root = Path(os.path.normpath(data_dir))
    try:
        rel = normalized.relative_to(data_root)
    except ValueError:
        return host_path
    return str(Path(host_data_dir) / rel)


def _translate_mounts(
    mounts: list[VolumeMount],
    *,
    data_dir: str,
    host_data_dir: str,
) -> list[VolumeMount]:
    """Apply DooD translation to every mount, preserving all other fields.

    Returns the input list unchanged (same objects) when translation is
    disabled. ``readonly`` and ``container_path`` are never touched —
    only the bind SOURCE is rewritten.
    """
    if not host_data_dir:
        return mounts
    translated: list[VolumeMount] = []
    for m in mounts:
        new_source = _translate_bind_source(
            m.host_path, data_dir=data_dir, host_data_dir=host_data_dir
        )
        if new_source == m.host_path and not _is_under(m.host_path, data_dir):
            logger.warning(
                "DooD: bind source outside DATA_DIR passed through "
                "untranslated. The host dockerd resolves it against the "
                "HOST filesystem; the containerized orchestrator cannot "
                "verify it exists, and dockerd silently creates an empty "
                "root-owned directory for a missing source.",
                host_path=m.host_path,
                container_path=m.container_path,
            )
            translated.append(m)
        else:
            translated.append(dataclasses.replace(m, host_path=new_source))
    return translated


def _is_under(path: str, root: str) -> bool:
    """Lexical containment check (mirrors _translate_bind_source)."""
    try:
        Path(os.path.normpath(path)).relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# verify_infrastructure support (declarative-infra design §4.2).
#
# Retry budget for the checks that race the deployment layer's cold
# start: compose starts the gateway before the orchestrator, but a
# Python container needs a few seconds to finish imports and bind its
# listeners. ~60s total at 2s intervals absorbs a slow host without
# masking a genuinely-down service for long. Static checks (network
# existence/shape) never retry — waiting cannot create a network.
# ---------------------------------------------------------------------------

_VERIFY_RETRY_BUDGET_S: float = 60.0
_VERIFY_RETRY_INTERVAL_S: float = 2.0

_COMPOSE_HINT: str = (
    "Infrastructure is declared by the deployment layer, not created by "
    "the orchestrator — run: "
    "docker compose -f deploy/compose/compose.yaml up -d"
)


async def _retry_within_budget(
    check: Callable[[], Awaitable[None]],
    *,
    what: str,
) -> None:
    """Run *check* until it passes or the retry budget elapses.

    Budget/interval are read per call so tests can patch the module
    constants. The final failure re-raises the last check error wrapped
    with the budget context — the inner message already carries the
    fix-it hint.
    """
    deadline = time.monotonic() + _VERIFY_RETRY_BUDGET_S
    attempt = 0
    while True:
        attempt += 1
        try:
            await check()
            return
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                msg = (
                    f"{what}: still failing after {attempt} attempts "
                    f"(~{_VERIFY_RETRY_BUDGET_S:.0f}s budget). {exc}"
                )
                raise RuntimeError(msg) from exc
            logger.debug(
                "verify_infrastructure check not passing yet — retrying",
                what=what,
                attempt=attempt,
                error=str(exc),
            )
            await asyncio.sleep(_VERIFY_RETRY_INTERVAL_S)


async def _get_network_info(
    client: aiodocker.Docker, network_name: str
) -> dict[str, Any] | None:
    """Inspect a network by name; None when it does not exist."""
    try:
        network = await client.networks.get(network_name)
    except aiodocker.exceptions.DockerError as exc:
        if exc.status == 404:
            return None
        raise
    info: dict[str, Any] = await network.show()
    return info


async def _check_gateway_ip(
    client: aiodocker.Docker,
    *,
    container_name: str,
    network_name: str,
    expected_ip: str,
) -> None:
    """Invariant (c): the gateway container sits on the agent network
    at exactly the configured ``EGRESS_GATEWAY_DNS_IP``.

    Agents get *expected_ip* pinned as their DNS resolver; if the
    running gateway actually holds a different address every agent
    spawn would silently lose DNS, so a drifted deployment must refuse
    to start instead.
    """
    try:
        container = client.containers.container(container_name)
        info: dict[str, Any] = await container.show()
    except aiodocker.exceptions.DockerError as exc:
        raise RuntimeError(
            f"Egress gateway container {container_name!r} not found. "
            f"{_COMPOSE_HINT}"
        ) from exc

    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    attachment = networks.get(network_name)
    if not attachment:
        raise RuntimeError(
            f"Egress gateway container {container_name!r} is not attached "
            f"to the agent network {network_name!r}. {_COMPOSE_HINT}"
        )
    actual_ip = str(attachment.get("IPAddress") or "")
    if actual_ip != expected_ip:
        raise RuntimeError(
            f"Egress gateway agent-net address mismatch: configured "
            f"EGRESS_GATEWAY_DNS_IP={expected_ip!r} but the running "
            f"container holds {actual_ip!r}. Align the compose fixed IP "
            f"with the EGRESS_GATEWAY_DNS_IP env var. {_COMPOSE_HINT}"
        )


async def _check_http_healthz(url: str, *, hint: str) -> None:
    """Invariant (d): the gateway answers ``GET /healthz`` with 200.

    The orchestrator reaches the bridge subnet directly on a Linux
    host today and from inside a compose-attached container after
    S1-2 — same code, both phases.
    """
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as resp,
        ):
            status = resp.status
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        raise RuntimeError(
            f"Egress gateway healthz not reachable at {url}: {exc}. {hint}"
        ) from exc
    if status != 200:
        raise RuntimeError(
            f"Egress gateway healthz at {url} returned HTTP {status} "
            f"(expected 200). {hint}"
        )


async def _check_tcp_reachable(url: str, *, hint: str) -> None:
    """Invariant (e): a TCP connection to *url*'s host:port succeeds."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4222
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
    except (TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"NATS not reachable at {host}:{port} (from NATS_URL={url!r}): "
            f"{exc}. {hint}"
        ) from exc
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


class DockerContainerHandle:
    """Handle to a running Docker container."""

    def __init__(self, container: aiodocker.containers.DockerContainer, container_name: str) -> None:
        self._container = container
        self._name = container_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def pid(self) -> int:
        return hash(self._name) & 0x7FFFFFFF

    async def wait(self) -> int:
        result: dict[str, Any] = await self._container.wait()
        return int(result.get("StatusCode", -1))

    async def stop(self, timeout: int = 1) -> None:
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await self._container.stop(t=timeout)
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await self._container.delete(force=True)

    async def read_stderr(self) -> AsyncIterator[bytes]:
        async for line in self._container.log(stdout=False, stderr=True, follow=True):
            yield line.encode() if isinstance(line, str) else line


class DockerRuntime:
    """Docker Engine API implementation using aiodocker."""

    def __init__(self) -> None:
        self._client: aiodocker.Docker | None = None

    @property
    def name(self) -> str:
        return "docker"

    async def ensure_available(self) -> None:
        self._client = aiodocker.Docker()
        try:
            await self._client.system.info()
            logger.debug("Docker runtime available")
        except (OSError, aiodocker.exceptions.DockerError) as exc:
            await self._client.close()
            self._client = None
            msg = (
                "\n================================================================\n"
                "  FATAL: Docker daemon is not reachable                         \n"
                "                                                                \n"
                "  Agents cannot run without Docker. To fix:                     \n"
                "  1. Ensure Docker is installed and running                     \n"
                "  2. Run: docker info                                           \n"
                "  3. Restart RoleMesh                                           \n"
                "================================================================\n"
            )
            raise RuntimeError(msg) from exc

        await self._check_daemon_version()

    async def _check_daemon_version(self) -> None:
        """Fail fast if dockerd is older than the hardening floor (R5.1-1).

        Runs once per process, from ensure_available() at startup. If the
        daemon is replaced or upgraded mid-process, the new version is
        only picked up on a full orchestrator restart — acceptable, since
        operators typically restart rolemesh after dockerd upgrades anyway.
        """
        client = self._client
        if client is None:
            return
        try:
            info = await client.version()
        except aiodocker.exceptions.DockerError as exc:
            logger.warning(
                "Could not read dockerd version — proceeding without version gate",
                error=str(exc),
            )
            return
        version_str = str(info.get("Version", ""))
        parsed = _parse_docker_version(version_str)
        if parsed is None:
            logger.warning("Unparseable dockerd version — skipping gate", version=version_str)
            return
        if parsed < _MIN_DOCKERD_VERSION:
            msg = (
                f"dockerd {version_str} is below the hardening floor "
                f"{_MIN_DOCKERD_VERSION[0]}.{_MIN_DOCKERD_VERSION[1]}. "
                "Upgrade Docker."
            )
            raise IncompatibleDockerVersionError(msg)
        logger.info("dockerd version OK", version=version_str)

    def _ensure_client(self) -> aiodocker.Docker:
        if self._client is None:
            msg = "DockerRuntime.ensure_available() must be called first"
            raise RuntimeError(msg)
        return self._client

    async def run(self, spec: ContainerSpec) -> DockerContainerHandle:
        client = self._ensure_client()

        # DooD translation (docs/21 §7.1): rewrite bind sources under
        # DATA_DIR to their host-side equivalents before they reach the
        # host dockerd. No-op when ROLEMESH_HOST_DATA_DIR is empty
        # (host-process dev flow). Function-level import so tests can
        # monkeypatch the config module attributes.
        from rolemesh.core.config import DATA_DIR, ROLEMESH_HOST_DATA_DIR

        if ROLEMESH_HOST_DATA_DIR:
            spec = dataclasses.replace(
                spec,
                mounts=_translate_mounts(
                    spec.mounts,
                    data_dir=str(DATA_DIR),
                    host_data_dir=ROLEMESH_HOST_DATA_DIR,
                ),
            )

        config = self._spec_to_config(spec)

        # Remove existing container with same name (if any)
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            old = client.containers.container(spec.name)
            await old.delete(force=True)

        container = await client.containers.create_or_replace(
            name=spec.name,
            config=config,
        )
        await container.start()
        return DockerContainerHandle(container, spec.name)

    async def stop(self, name: str, timeout: int = 1) -> None:
        client = self._ensure_client()
        container = client.containers.container(name)
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await container.stop(t=timeout)
        with contextlib.suppress(aiodocker.exceptions.DockerError):
            await container.delete(force=True)

    async def cleanup_orphans(
        self,
        prefix: str,
        *,
        allowed_images: frozenset[str],
    ) -> list[str]:
        """Stop and remove containers whose name starts with ``prefix``
        AND whose image is in ``allowed_images``.

        INV-3 (cleanup-safety): name prefix alone is not a strong enough
        filter — a user might run an unrelated container whose name
        happens to overlap ours (e.g. ``rolemesh-`` prefixed local
        experiment) and we must never touch it. The image whitelist
        gives us a positive identity signal: only containers we
        actually launched (from images we control) qualify.

        ``allowed_images`` is taken from the caller rather than
        hard-coded so adding a new image type (e.g. a future runner
        flavor) updates one place and propagates here without changes
        in this module.
        """
        client = self._ensure_client()
        containers = await client.containers.list(
            all=True,
            filters={"name": [prefix]},
        )
        normalized_whitelist = {_normalize_image_ref(i) for i in allowed_images}
        removed: list[str] = []
        for c in containers:
            cname: str = c._container.get("Names", [""])[0].lstrip("/")
            if not cname.startswith(prefix):
                # Docker's ``name`` filter is a substring match, not a
                # prefix match. Re-check explicitly so a container
                # *containing* the prefix as a substring is not
                # mistaken for a prefix match.
                continue
            image_ref = c._container.get("Image", "")
            if _normalize_image_ref(image_ref) not in normalized_whitelist:
                continue
            await self.stop(cname)
            removed.append(cname)
        if removed:
            logger.info("Stopped orphaned containers", count=len(removed), names=removed)
        return removed

    async def list_live(self, prefix: str) -> set[str]:
        """Names of RUNNING containers matching ``prefix`` (reaper liveness oracle).

        ``all=False`` restricts the listing to running containers, so an
        exited/dead container is naturally absent. Docker's ``name`` filter is a
        substring match, so re-check the prefix explicitly (same guard as
        ``cleanup_orphans``). Read-only — never stops anything.
        """
        client = self._ensure_client()
        containers = await client.containers.list(
            all=False,
            filters={"name": [prefix]},
        )
        live: set[str] = set()
        for c in containers:
            cname: str = c._container.get("Names", [""])[0].lstrip("/")
            if cname.startswith(prefix):
                live.add(cname)
        return live

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    # -----------------------------------------------------------------
    # Infrastructure verification (declarative-infra design §4.2).
    #
    # The deployment layer (deploy/compose/compose.yaml today, Helm
    # later) declares networks + gateway + NATS; the orchestrator only
    # checks the declared invariants here — read-only, fail-closed —
    # and refuses to start when they do not hold. The old imperative
    # path (ensure_agent_network / ensure_egress_network /
    # probe-container reachability checks) is retired.
    # -----------------------------------------------------------------

    async def verify_infrastructure(self) -> None:
        """Verify the compose-declared infrastructure invariants.

        Invariants (in check order):
          (a) agent network exists and is ``Internal=true``;
          (b) egress network exists;
          (c) the gateway container's agent-net address equals
              ``EGRESS_GATEWAY_DNS_IP`` (the value agents get pinned
              as their DNS resolver);
          (d) ``GET http://<EGRESS_GATEWAY_DNS_IP>:<port>/healthz``
              returns 200. The orchestrator can reach the bridge
              subnet directly on a Linux host, and equally from inside
              a compose-attached container after S1-2 — the same code
              serves both phases;
          (e) NATS is TCP-reachable at ``NATS_URL``.

        (c)–(e) race the gateway/NATS cold start (compose starts them
        before the orchestrator, but "started" != "serving"), so they
        retry within a bounded budget. (a)/(b) are static — a missing
        network never heals by waiting — and fail immediately.
        """
        from rolemesh.core.config import (
            CONTAINER_EGRESS_NETWORK_NAME,
            CONTAINER_NETWORK_NAME,
            CREDENTIAL_PROXY_PORT,
            DATA_DIR,
            EGRESS_GATEWAY_CONTAINER_NAME,
            EGRESS_GATEWAY_DNS_IP,
            NATS_URL,
            ROLEMESH_HOST_DATA_DIR,
        )

        client = self._ensure_client()

        # (a) agent network: exists + Internal=true.
        agent_info = await _get_network_info(client, CONTAINER_NETWORK_NAME)
        if agent_info is None:
            raise RuntimeError(
                f"Agent network {CONTAINER_NETWORK_NAME!r} does not exist. "
                f"{_COMPOSE_HINT}"
            )
        if not bool(agent_info.get("Internal", False)):
            raise RuntimeError(
                f"Agent network {CONTAINER_NETWORK_NAME!r} exists but is not "
                "Internal=true — agents would have a direct route to the "
                "internet, defeating egress control. Recreate it: "
                f"docker network rm {CONTAINER_NETWORK_NAME}; {_COMPOSE_HINT}"
            )

        # (b) egress network: exists.
        if await _get_network_info(client, CONTAINER_EGRESS_NETWORK_NAME) is None:
            raise RuntimeError(
                f"Egress network {CONTAINER_EGRESS_NETWORK_NAME!r} does not "
                f"exist — the gateway has no route out. {_COMPOSE_HINT}"
            )

        # (c) gateway holds the configured agent-net address.
        await _retry_within_budget(
            lambda: _check_gateway_ip(
                client,
                container_name=EGRESS_GATEWAY_CONTAINER_NAME,
                network_name=CONTAINER_NETWORK_NAME,
                expected_ip=EGRESS_GATEWAY_DNS_IP,
            ),
            what="egress gateway agent-net address",
        )

        # (d) gateway healthz answers 200.
        healthz_url = (
            f"http://{EGRESS_GATEWAY_DNS_IP}:{CREDENTIAL_PROXY_PORT}/healthz"
        )
        await _retry_within_budget(
            lambda: _check_http_healthz(healthz_url, hint=_COMPOSE_HINT),
            what="egress gateway /healthz",
        )

        # (e) NATS TCP-reachable at the orchestrator-facing URL.
        await _retry_within_budget(
            lambda: _check_tcp_reachable(NATS_URL, hint=_COMPOSE_HINT),
            what="NATS reachability",
        )

        # (f) DooD loopback self-check — only when path translation is
        # active (ROLEMESH_HOST_DATA_DIR set, i.e. the orchestrator runs
        # in a container). Required by the docs/21 §11 risk table: a
        # misconfigured ROLEMESH_HOST_DATA_DIR would make every agent
        # spawn bind empty dockerd-created directories instead of the
        # real data tree, silently. This is the ONE deliberate exception
        # to "verify_infrastructure never spawns containers": the
        # invariant under test ("the translated host path and DATA_DIR
        # name the same directory") is unobservable from inside the
        # orchestrator container without a probe container.
        if ROLEMESH_HOST_DATA_DIR:
            await self._verify_dood_translation(
                data_dir=str(DATA_DIR),
                host_data_dir=ROLEMESH_HOST_DATA_DIR,
            )

        logger.info(
            "Infrastructure verified",
            agent_network=CONTAINER_NETWORK_NAME,
            egress_network=CONTAINER_EGRESS_NETWORK_NAME,
            gateway_dns_ip=EGRESS_GATEWAY_DNS_IP,
            dood_translation="on" if ROLEMESH_HOST_DATA_DIR else "off",
        )

    async def _verify_dood_translation(
        self,
        *,
        data_dir: str,
        host_data_dir: str,
    ) -> None:
        """DooD loopback self-check (docs/21 §11 risk: translation misconfig).

        Write a sentinel with unique content under DATA_DIR, bind the
        TRANSLATED host path of its directory into a one-shot probe
        container, and read the content back. A mismatch (or a probe
        that cannot read the file at all) proves ROLEMESH_HOST_DATA_DIR
        does not name the host directory actually mounted on DATA_DIR —
        refuse to start. NetworkMode=none: the probe only reads a file.
        """
        from rolemesh.core.config import CONTAINER_IMAGE

        client = self._ensure_client()
        token = uuid.uuid4().hex
        probe_dir = Path(data_dir) / f".dood-probe-{token}"
        # Name carries the "rolemesh-" prefix + an allowed image so a
        # leaked probe (orchestrator killed mid-check) is reaped by the
        # next startup's cleanup_orphans.
        probe_name = f"rolemesh-dood-probe-{token[:12]}"
        sentinel_container_path = "/dood-probe/sentinel"
        translated_dir = _translate_bind_source(
            str(probe_dir), data_dir=data_dir, host_data_dir=host_data_dir
        )

        container = None
        try:
            probe_dir.mkdir(parents=True, exist_ok=False)
            (probe_dir / "sentinel").write_text(token, encoding="utf-8")

            config: dict[str, Any] = {
                "Image": CONTAINER_IMAGE,
                "Entrypoint": ["cat", sentinel_container_path],
                "HostConfig": {
                    "Binds": [f"{translated_dir}:/dood-probe:ro"],
                    "NetworkMode": "none",
                    "CapDrop": ["ALL"],
                    "ReadonlyRootfs": True,
                },
            }
            container = await client.containers.create_or_replace(
                name=probe_name, config=config
            )
            await container.start()
            result: dict[str, Any] = await asyncio.wait_for(
                container.wait(), timeout=60
            )
            exit_code = int(result.get("StatusCode", -1))
            log_lines = await container.log(stdout=True, stderr=True)
            output = "".join(
                line if isinstance(line, str) else line.decode()
                for line in log_lines
            ).strip()
        except (TimeoutError, OSError, aiodocker.exceptions.DockerError) as exc:
            raise RuntimeError(
                f"DooD loopback self-check failed to run its probe "
                f"container: {exc}. Check that CONTAINER_IMAGE "
                f"({CONTAINER_IMAGE!r}) is built and the docker socket "
                f"is usable from the orchestrator container."
            ) from exc
        finally:
            if container is not None:
                with contextlib.suppress(aiodocker.exceptions.DockerError):
                    await container.delete(force=True)
            shutil.rmtree(probe_dir, ignore_errors=True)

        if exit_code != 0 or output != token:
            raise RuntimeError(
                "DooD path translation self-check FAILED: a probe "
                f"container bound the translated host path "
                f"{translated_dir!r} but read "
                f"{'nothing' if not output else output[:80]!r} instead of "
                f"the sentinel (probe exit code {exit_code}). "
                f"ROLEMESH_HOST_DATA_DIR ({host_data_dir!r}) does not "
                f"name the HOST directory that is bind-mounted onto "
                f"DATA_DIR ({data_dir!r}). Note: dockerd silently creates "
                "an empty root-owned directory for a missing bind source, "
                "so a wrong value fails exactly like this instead of "
                "erroring at mount time. Fix ROLEMESH_HOST_DATA_DIR in "
                ".env (absolute host path of the repo's data/ directory)."
            )
        logger.info(
            "DooD loopback self-check passed",
            host_data_dir=host_data_dir,
            data_dir=data_dir,
        )

    @staticmethod
    def _spec_to_config(spec: ContainerSpec) -> dict[str, Any]:
        """Convert a ContainerSpec to a Docker API config dict."""
        binds = _mounts_to_binds(spec.mounts)
        host_config: dict[str, Any] = {
            "Binds": binds,
            # Hardening defaults. Lists come from ContainerSpec so callers
            # can only *add* to cap_add / security_opt; the baseline of
            # dropping ALL capabilities and readonly rootfs is not opt-out
            # from the per-call spec path.
            "CapDrop": list(spec.cap_drop),
            "CapAdd": list(spec.cap_add),
            "SecurityOpt": list(spec.security_opt),
            "ReadonlyRootfs": bool(spec.readonly_rootfs),
        }

        # AutoRemove races with wait/inspect, so we skip it and
        # delete explicitly in the handle's stop() method.
        if spec.memory_limit:
            memory_bytes = _parse_memory(spec.memory_limit)
            host_config["Memory"] = memory_bytes
            # MemorySwap semantics (Docker API):
            #   unset or -1  → unlimited swap (bad: defeats memory limit)
            #   equal to Memory → swap disabled (what we want)
            # See https://docs.docker.com/engine/containers/resource_constraints/
            if spec.memory_swap is None:
                host_config["MemorySwap"] = memory_bytes
            else:
                host_config["MemorySwap"] = spec.memory_swap
        if spec.memory_swappiness is not None:
            host_config["MemorySwappiness"] = int(spec.memory_swappiness)

        if spec.cpu_limit:
            host_config["NanoCpus"] = int(spec.cpu_limit * 1e9)
        if spec.extra_hosts:
            host_config["ExtraHosts"] = [f"{h}:{ip}" for h, ip in spec.extra_hosts.items()]

        if spec.tmpfs:
            host_config["Tmpfs"] = dict(spec.tmpfs)
        if spec.pids_limit is not None:
            host_config["PidsLimit"] = int(spec.pids_limit)
        if spec.ulimits:
            host_config["Ulimits"] = [dict(u) for u in spec.ulimits]

        if spec.network_name:
            host_config["NetworkMode"] = spec.network_name

        # EC-2: agent containers get the egress gateway's IP pinned as
        # their DNS resolver so DNS queries flow through the
        # authoritative resolver (and thus the Safety pipeline). An
        # empty list leaves Docker's embedded DNS in place, which is
        # appropriate for the gateway container itself.
        if spec.dns:
            host_config["Dns"] = list(spec.dns)

        # OCI runtime (R1). Docker only honours HostConfig.Runtime for values
        # registered in /etc/docker/daemon.json — setting "runsc" on a host
        # without gVisor installed will fail at container create time, which
        # is the correct behaviour (fail closed).
        if spec.runtime:
            host_config["Runtime"] = spec.runtime

        config: dict[str, Any] = {
            "Image": spec.image,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "HostConfig": host_config,
            "Stdin": False,
            "AttachStdin": False,
            "AttachStdout": False,
            "AttachStderr": True,
            "OpenStdin": False,
        }

        if spec.user:
            config["User"] = spec.user
        if spec.entrypoint:
            config["Entrypoint"] = spec.entrypoint

        return config
