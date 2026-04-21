"""Docker Engine API implementation using aiodocker.

Replaces all subprocess-based Docker calls with the Docker Engine API.
"""

from __future__ import annotations

import contextlib
import os
import re
from typing import TYPE_CHECKING, Any

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
    from collections.abc import AsyncIterator

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
                "Upgrade Docker, or set CONTAINER_NETWORK_NAME='' to fall "
                "back to the default bridge (at the cost of losing custom-"
                "network isolation)."
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
        self, prefix: str, *, exclude_infra: bool = True,
    ) -> list[str]:
        client = self._ensure_client()
        containers = await client.containers.list(
            all=True,
            filters={"name": [prefix]},
        )
        # Skip infrastructure containers managed by docker compose
        _infra_suffixes = ("-postgres-", "-nats-", "-redis-")
        removed: list[str] = []
        for c in containers:
            cname: str = c._container.get("Names", [""])[0].lstrip("/")
            if exclude_infra and any(s in cname for s in _infra_suffixes):
                continue
            await self.stop(cname)
            removed.append(cname)
        if removed:
            logger.info("Stopped orphaned containers", count=len(removed), names=removed)
        return removed

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    # -----------------------------------------------------------------
    # Network hardening hooks (R5 / R5.1). Thin adapters over the pure
    # functions in container.network so tests can target either layer.
    # -----------------------------------------------------------------

    async def ensure_agent_network(self, network_name: str) -> None:
        from rolemesh.container.network import ensure_agent_network

        await ensure_agent_network(self._ensure_client(), network_name)

    async def verify_proxy_reachable(self, network_name: str, proxy_port: int) -> None:
        from rolemesh.container.network import verify_proxy_reachable

        await verify_proxy_reachable(self._ensure_client(), network_name, proxy_port)

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
