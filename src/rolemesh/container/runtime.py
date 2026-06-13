"""Container runtime abstraction.

Defines the ContainerRuntime Protocol and supporting types (ContainerSpec,
ContainerHandle, VolumeMount).  Concrete implementations live in separate
modules (e.g. docker_runtime.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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

    # Hardening — network. None keeps the runtime's default network
    # (backward compat); production runs set the isolated agent network
    # declared by the deployment layer.
    network_name: str | None = None

    # Hardening — explicit DNS servers. When set, the runtime overrides
    # its default resolver with these IPs. EC-2 points agent containers
    # at the egress gateway's authoritative resolver so DNS exfil
    # attempts go through the Safety pipeline. An empty list keeps the
    # default — appropriate for the gateway container itself and any
    # non-agent container.
    dns: list[str] = field(default_factory=list)

    # Hardening — OCI runtime selection: "runc" (default) | "runsc" (gVisor).
    # None means "let Docker pick its default runtime", which is backward-
    # compatible with existing deployments that have not registered runsc.
    runtime: str | None = None


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

    async def verify_infrastructure(self) -> None:
        """Verify the deployment layer's promises (design §4.2).

        Static infrastructure (networks, gateway, NATS) is declared by
        the deployment layer (docker compose / Helm), never created by
        application code. This check is strictly READ-ONLY and
        fail-closed: any missing invariant raises with a message that
        tells the operator how to fix the deployment; the orchestrator
        then refuses to start. No degradation, no self-bootstrap, no
        repair.
        """
        ...

    async def run(self, spec: ContainerSpec) -> ContainerHandle: ...

    async def stop(self, name: str, timeout: int = 1) -> None: ...

    async def cleanup_orphans(
        self, prefix: str, *, allowed_images: frozenset[str]
    ) -> list[str]: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_runtime(runtime_name: str | None = None) -> ContainerRuntime:
    """Create a ContainerRuntime from name (defaults to CONTAINER_BACKEND env var).

    Note: this selects the runtime abstraction backend (Docker vs K8s), *not*
    the OCI runtime. OCI runtime (runc/runsc) is controlled by
    CONTAINER_OCI_RUNTIME and applied per-container via ContainerSpec.runtime.
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
