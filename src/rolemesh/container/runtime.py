"""Container runtime abstraction.

Defines the ContainerRuntime Protocol and supporting types (ContainerSpec,
ContainerHandle, VolumeMount).  Concrete implementations live in separate
modules (e.g. docker_runtime.py).

Platform helpers for proxy bind-host and host-gateway detection are kept as
module-level functions so they can be used regardless of runtime.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from rolemesh.core.logger import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VolumeMount:
    """A bind mount specification for container execution."""

    host_path: str
    container_path: str
    readonly: bool


@dataclass(frozen=True)
class ContainerSpec:
    """Full specification for running a container.

    Hardening fields (cap_drop, security_opt, readonly_rootfs, tmpfs, pids_limit,
    memory_swap*, ulimits) default to safe values. All additions are backward
    compatible — existing call sites that only set name/image/env remain valid.
    """

    name: str
    image: str
    mounts: list[VolumeMount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    user: str | None = None  # "uid:gid"
    memory_limit: str | None = None  # "512m"
    cpu_limit: float | None = None  # 1.0
    extra_hosts: dict[str, str] = field(default_factory=dict)
    remove_on_exit: bool = True
    entrypoint: list[str] | None = None

    # Hardening — capabilities / LSM
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    cap_add: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)

    # Hardening — filesystem
    readonly_rootfs: bool = True
    tmpfs: dict[str, str] = field(default_factory=dict)  # {"/tmp": "size=64m"}

    # Hardening — resource ceilings
    pids_limit: int | None = 512
    memory_swap: int | None = None  # bytes; when set equal to Memory, disables swap
    memory_swappiness: int | None = 0
    ulimits: list[dict[str, object]] = field(default_factory=list)

    # Hardening — network. None keeps Docker's default bridge (backward compat);
    # production runs should set this to a custom bridge with enable_icc=false.
    network_name: str | None = None


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ContainerHandle(Protocol):
    """Handle to a running container."""

    @property
    def name(self) -> str: ...

    @property
    def pid(self) -> int:
        """Process ID (or container ID hash) for tracking."""
        ...

    async def wait(self) -> int:
        """Wait for exit, return exit code."""
        ...

    async def stop(self, timeout: int = 1) -> None:
        """Stop the container."""
        ...

    def read_stderr(self) -> AsyncIterator[bytes]:
        """Stream stderr for logging."""
        ...


class ContainerRuntime(Protocol):
    """Protocol for container execution backends."""

    @property
    def name(self) -> str: ...

    async def ensure_available(self) -> None: ...

    async def run(self, spec: ContainerSpec) -> ContainerHandle: ...

    async def stop(self, name: str, timeout: int = 1) -> None: ...

    async def cleanup_orphans(self, prefix: str) -> list[str]: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_HOST_GATEWAY: str = "host.docker.internal"


# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------


def _detect_proxy_bind_host() -> str:
    """Detect the appropriate bind host for the credential proxy.

    Docker Desktop (macOS): 127.0.0.1 -- the VM routes host.docker.internal to loopback.
    Docker (Linux): bind to the docker0 bridge IP so only containers can reach it.
    """
    if platform.system() == "Darwin":
        return "127.0.0.1"

    # WSL uses Docker Desktop -- loopback is correct
    if Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        return "127.0.0.1"

    # Bare-metal Linux: try to find docker0 bridge IP
    try:
        import fcntl
        import socket
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            addr = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", b"docker0"[:15]))  # SIOCGIFADDR
            return socket.inet_ntoa(addr[20:24])
        except OSError:
            pass
        finally:
            sock.close()
    except ImportError:
        pass

    return "0.0.0.0"


PROXY_BIND_HOST: str = os.environ.get("CREDENTIAL_PROXY_HOST") or _detect_proxy_bind_host()


def detect_proxy_bind_host() -> str:
    """Public API for proxy bind host detection."""
    return PROXY_BIND_HOST


def get_host_gateway_extra_hosts() -> dict[str, str]:
    """Extra hosts needed for containers to resolve the host gateway."""
    if platform.system() == "Linux":
        return {"host.docker.internal": "host-gateway"}
    return {}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_runtime(runtime_name: str | None = None) -> ContainerRuntime:
    """Create a ContainerRuntime from name (defaults to CONTAINER_BACKEND env var).

    Note: this selects the runtime abstraction backend (Docker vs K8s), *not*
    the OCI runtime. OCI runtime (runc/runsc) is controlled by CONTAINER_RUNTIME
    and applied per-container via ContainerSpec.runtime.
    """
    from rolemesh.core.config import CONTAINER_BACKEND

    name = runtime_name or CONTAINER_BACKEND

    if name == "docker":
        from rolemesh.container.docker_runtime import DockerRuntime

        return DockerRuntime()

    if name == "k8s":
        msg = "Kubernetes runtime is not yet implemented"
        raise NotImplementedError(msg)

    msg = f"Unknown container backend: {name}"
    raise ValueError(msg)
