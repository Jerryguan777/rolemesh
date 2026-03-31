"""Tests for rolemesh.container.runtime -- Protocol types and factory."""

from __future__ import annotations

import pytest

from rolemesh.container.runtime import (
    CONTAINER_HOST_GATEWAY,
    ContainerSpec,
    VolumeMount,
    get_host_gateway_extra_hosts,
    get_runtime,
)


def test_container_host_gateway() -> None:
    assert CONTAINER_HOST_GATEWAY == "host.docker.internal"


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


def test_container_spec_frozen() -> None:
    spec = ContainerSpec(name="test", image="test:latest")
    try:
        spec.name = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_get_host_gateway_extra_hosts() -> None:
    result = get_host_gateway_extra_hosts()
    assert isinstance(result, dict)


def test_get_runtime_docker() -> None:
    rt = get_runtime("docker")
    assert rt.name == "docker"


def test_get_runtime_k8s_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        get_runtime("k8s")


def test_get_runtime_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown container runtime"):
        get_runtime("podman")
