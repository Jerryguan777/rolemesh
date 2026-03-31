"""Docker Engine API implementation using aiodocker.

Replaces all subprocess-based Docker calls with the Docker Engine API.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import aiodocker
import aiodocker.containers
import aiodocker.exceptions

from rolemesh.core.logger import get_logger

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


def _mounts_to_binds(mounts: list[VolumeMount]) -> list[str]:
    """Convert VolumeMount list to Docker bind strings."""
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

    @staticmethod
    def _spec_to_config(spec: ContainerSpec) -> dict[str, Any]:
        """Convert a ContainerSpec to a Docker API config dict."""
        binds = _mounts_to_binds(spec.mounts)
        host_config: dict[str, Any] = {
            "Binds": binds,
        }

        # AutoRemove races with wait/inspect, so we skip it and
        # delete explicitly in the handle's stop() method.
        if spec.memory_limit:
            host_config["Memory"] = _parse_memory(spec.memory_limit)
        if spec.cpu_limit:
            host_config["NanoCpus"] = int(spec.cpu_limit * 1e9)
        if spec.extra_hosts:
            host_config["ExtraHosts"] = [f"{h}:{ip}" for h, ip in spec.extra_hosts.items()]

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
