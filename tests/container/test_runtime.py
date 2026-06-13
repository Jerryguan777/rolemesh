"""Tests for rolemesh.container.runtime -- Protocol types and factory."""

from __future__ import annotations

import pytest

from rolemesh.container.runtime import (
    ContainerSpec,
    VolumeMount,
    get_runtime,
)


def test_volume_mount_frozen() -> None:
    m = VolumeMount(host_path="/a", container_path="/b", readonly=True)
    try:
        m.host_path = "/c"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_container_spec_defaults() -> None:
    spec = ContainerSpec(name="test", image="test:latest")
    assert spec.mounts == []
    assert spec.env == {}
    assert spec.user is None
    assert spec.memory_limit is None
    assert spec.cpu_limit is None
    assert spec.extra_hosts == {}
    assert spec.remove_on_exit is True
    assert spec.entrypoint is None
    # Hardening defaults: safe out of the box.
    assert spec.cap_drop == ["ALL"]
    assert spec.cap_add == []
    assert spec.security_opt == []
    assert spec.readonly_rootfs is True
    assert spec.tmpfs == {}
    assert spec.pids_limit == 512
    assert spec.memory_swap is None
    assert spec.memory_swappiness == 0
    assert spec.ulimits == []


def test_container_spec_cap_drop_isolation_between_instances() -> None:
    """Default factory must not share the list between instances (mutable default trap)."""
    a = ContainerSpec(name="a", image="i")
    b = ContainerSpec(name="b", image="i")
    assert a.cap_drop is not b.cap_drop


def test_container_spec_frozen() -> None:
    spec = ContainerSpec(name="test", image="test:latest")
    try:
        spec.name = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_get_runtime_docker() -> None:
    rt = get_runtime("docker")
    assert rt.name == "docker"


def test_get_runtime_k8s_returns_k8s_backend() -> None:
    """k8s branch wires to K8sRuntime (kubernetes_asyncio is installed here).

    Mutation guard: if get_runtime's k8s branch regressed to
    ``raise NotImplementedError`` (or returned the docker backend), this
    fails. Constructing K8sRuntime() does no I/O — ensure_available() is
    what touches the cluster — so this is safe without a live API server.
    """
    pytest.importorskip("kubernetes_asyncio")
    rt = get_runtime("k8s")
    assert rt.name == "k8s"


def test_get_runtime_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown container runtime"):
        get_runtime("podman")
