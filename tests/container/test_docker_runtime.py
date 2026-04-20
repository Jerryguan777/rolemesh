"""Tests for rolemesh.container.docker_runtime -- DockerRuntime with mocked aiodocker."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rolemesh.container.docker_runtime import (
    DockerContainerHandle,
    DockerRuntime,
    _mounts_to_binds,
    _parse_memory,
)
from rolemesh.container.runtime import ContainerSpec, VolumeMount

# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def test_parse_memory_bytes() -> None:
    assert _parse_memory("1024") == 1024


def test_parse_memory_k() -> None:
    assert _parse_memory("512k") == 512 * 1024


def test_parse_memory_m() -> None:
    assert _parse_memory("256m") == 256 * 1024**2


def test_parse_memory_g() -> None:
    assert _parse_memory("2g") == 2 * 1024**3


def test_mounts_to_binds() -> None:
    mounts = [
        VolumeMount(host_path="/a", container_path="/b", readonly=True),
        VolumeMount(host_path="/c", container_path="/d", readonly=False),
    ]
    result = _mounts_to_binds(mounts)
    assert result == ["/a:/b:ro", "/c:/d:rw"]


# ---------------------------------------------------------------------------
# DockerContainerHandle
# ---------------------------------------------------------------------------


def _make_mock_container() -> MagicMock:
    container = MagicMock()
    container.wait = AsyncMock(return_value={"StatusCode": 0})
    container.stop = AsyncMock()
    container.delete = AsyncMock()
    return container


async def test_handle_name() -> None:
    container = _make_mock_container()
    handle = DockerContainerHandle(container, "test-container")
    assert handle.name == "test-container"


async def test_handle_pid() -> None:
    container = _make_mock_container()
    handle = DockerContainerHandle(container, "test-container")
    assert isinstance(handle.pid, int)
    assert handle.pid >= 0


async def test_handle_wait_success() -> None:
    container = _make_mock_container()
    handle = DockerContainerHandle(container, "test")
    code = await handle.wait()
    assert code == 0


async def test_handle_wait_error() -> None:
    container = _make_mock_container()
    container.wait = AsyncMock(return_value={"StatusCode": 1})
    handle = DockerContainerHandle(container, "test")
    code = await handle.wait()
    assert code == 1


async def test_handle_stop() -> None:
    container = _make_mock_container()
    handle = DockerContainerHandle(container, "test")
    await handle.stop(timeout=5)
    container.stop.assert_awaited_once_with(t=5)
    container.delete.assert_awaited_once_with(force=True)


# ---------------------------------------------------------------------------
# DockerRuntime
# ---------------------------------------------------------------------------


async def test_runtime_name() -> None:
    rt = DockerRuntime()
    assert rt.name == "docker"


async def test_ensure_available_success() -> None:
    rt = DockerRuntime()
    mock_docker = MagicMock()
    mock_docker.system = MagicMock()
    mock_docker.system.info = AsyncMock()
    mock_docker.close = AsyncMock()

    with patch("rolemesh.container.docker_runtime.aiodocker.Docker", return_value=mock_docker):
        await rt.ensure_available()
    assert rt._client is not None


async def test_ensure_available_failure() -> None:
    rt = DockerRuntime()
    mock_docker = MagicMock()
    mock_docker.system = MagicMock()
    mock_docker.system.info = AsyncMock(side_effect=OSError("not running"))
    mock_docker.close = AsyncMock()

    with (
        patch("rolemesh.container.docker_runtime.aiodocker.Docker", return_value=mock_docker),
        pytest.raises(RuntimeError, match="Docker daemon"),
    ):
        await rt.ensure_available()
    assert rt._client is None


async def test_run_creates_and_starts() -> None:
    rt = DockerRuntime()
    mock_container = _make_mock_container()
    mock_container.start = AsyncMock()

    mock_client = MagicMock()
    mock_client.containers = MagicMock()
    import aiodocker.exceptions

    mock_client.containers.container = MagicMock(side_effect=aiodocker.exceptions.DockerError(404, "not found"))
    mock_client.containers.create_or_replace = AsyncMock(return_value=mock_container)
    rt._client = mock_client

    spec = ContainerSpec(
        name="test-run",
        image="test:latest",
        env={"FOO": "bar"},
    )
    handle = await rt.run(spec)
    assert handle.name == "test-run"
    mock_container.start.assert_awaited_once()


async def test_stop_idempotent() -> None:
    rt = DockerRuntime()
    mock_container = _make_mock_container()

    mock_client = MagicMock()
    mock_client.containers = MagicMock()
    mock_client.containers.container = MagicMock(return_value=mock_container)
    rt._client = mock_client

    await rt.stop("test-stop")
    mock_container.stop.assert_awaited_once()
    mock_container.delete.assert_awaited_once()


async def test_cleanup_orphans() -> None:
    rt = DockerRuntime()

    mock_c1 = MagicMock()
    mock_c1._container = {"Names": ["/rolemesh-test-1"]}

    mock_client = MagicMock()
    mock_client.containers = MagicMock()
    mock_client.containers.list = AsyncMock(return_value=[mock_c1])

    # Mock stop to be a no-op
    mock_stopped = _make_mock_container()
    mock_client.containers.container = MagicMock(return_value=mock_stopped)
    rt._client = mock_client

    removed = await rt.cleanup_orphans("rolemesh-")
    assert removed == ["rolemesh-test-1"]


async def test_close() -> None:
    rt = DockerRuntime()
    mock_client = MagicMock()
    mock_client.close = AsyncMock()
    rt._client = mock_client

    await rt.close()
    mock_client.close.assert_awaited_once()
    assert rt._client is None


async def test_close_no_client() -> None:
    rt = DockerRuntime()
    await rt.close()  # Should not raise


def test_spec_to_config_basic() -> None:
    spec = ContainerSpec(
        name="test",
        image="img:latest",
        env={"K": "V"},
    )
    config: dict[str, Any] = DockerRuntime._spec_to_config(spec)
    assert config["Image"] == "img:latest"
    assert "K=V" in config["Env"]


def test_spec_to_config_user() -> None:
    spec = ContainerSpec(name="test", image="img", user="1000:1000")
    config: dict[str, Any] = DockerRuntime._spec_to_config(spec)
    assert config["User"] == "1000:1000"


def test_spec_to_config_entrypoint() -> None:
    spec = ContainerSpec(name="test", image="img", entrypoint=["python", "-m", "app"])
    config: dict[str, Any] = DockerRuntime._spec_to_config(spec)
    assert config["Entrypoint"] == ["python", "-m", "app"]


def test_spec_to_config_memory() -> None:
    spec = ContainerSpec(name="test", image="img", memory_limit="512m")
    config: dict[str, Any] = DockerRuntime._spec_to_config(spec)
    assert config["HostConfig"]["Memory"] == 512 * 1024**2


def test_spec_to_config_cpu() -> None:
    spec = ContainerSpec(name="test", image="img", cpu_limit=1.5)
    config: dict[str, Any] = DockerRuntime._spec_to_config(spec)
    assert config["HostConfig"]["NanoCpus"] == int(1.5e9)


def test_spec_to_config_extra_hosts() -> None:
    spec = ContainerSpec(name="test", image="img", extra_hosts={"host.docker.internal": "host-gateway"})
    config: dict[str, Any] = DockerRuntime._spec_to_config(spec)
    assert "host.docker.internal:host-gateway" in config["HostConfig"]["ExtraHosts"]


# ---------------------------------------------------------------------------
# Hardening (R3, R4, R7) — defense-in-depth fields surface on HostConfig
# ---------------------------------------------------------------------------


def test_spec_to_config_cap_drop_all() -> None:
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["CapDrop"] == ["ALL"]
    assert hc["CapAdd"] == []


def test_spec_to_config_readonly_rootfs_default_true() -> None:
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["ReadonlyRootfs"] is True


def test_spec_to_config_readonly_rootfs_opt_out() -> None:
    """Callers can still disable readonly rootfs when explicitly needed."""
    spec = ContainerSpec(name="t", image="i", readonly_rootfs=False)
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["ReadonlyRootfs"] is False


def test_spec_to_config_security_opts_passthrough() -> None:
    spec = ContainerSpec(
        name="t", image="i",
        security_opt=["no-new-privileges:true", "apparmor=docker-default"],
    )
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert "no-new-privileges:true" in hc["SecurityOpt"]
    assert "apparmor=docker-default" in hc["SecurityOpt"]


def test_spec_to_config_no_seccomp_unconfined() -> None:
    """We must never silently disable seccomp. Default: no seccomp entry
    at all → Docker applies embedded default profile."""
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert not any("seccomp=unconfined" in opt for opt in hc["SecurityOpt"])


def test_spec_to_config_tmpfs() -> None:
    spec = ContainerSpec(name="t", image="i", tmpfs={"/tmp": "rw,size=64m"})
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["Tmpfs"] == {"/tmp": "rw,size=64m"}


def test_spec_to_config_tmpfs_absent_when_empty() -> None:
    """Don't emit empty Tmpfs — some Docker versions reject {}."""
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert "Tmpfs" not in hc


def test_spec_to_config_pids_limit_default() -> None:
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["PidsLimit"] == 512


def test_spec_to_config_pids_limit_override() -> None:
    spec = ContainerSpec(name="t", image="i", pids_limit=128)
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["PidsLimit"] == 128


def test_spec_to_config_pids_limit_none_drops_key() -> None:
    spec = ContainerSpec(name="t", image="i", pids_limit=None)
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert "PidsLimit" not in hc


def test_spec_to_config_memory_swap_disabled_when_memory_set() -> None:
    """Default behaviour: setting memory_limit without memory_swap disables swap.
    MemorySwap == Memory → swap off. Not setting MemorySwap lets cgroups default
    to unlimited swap, which defeats the memory cap."""
    spec = ContainerSpec(name="t", image="i", memory_limit="512m")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["Memory"] == 512 * 1024**2
    assert hc["MemorySwap"] == hc["Memory"]


def test_spec_to_config_memory_swappiness_zero() -> None:
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["MemorySwappiness"] == 0


def test_spec_to_config_ulimits_passthrough() -> None:
    spec = ContainerSpec(
        name="t", image="i",
        ulimits=[{"Name": "nofile", "Soft": 1024, "Hard": 2048}],
    )
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["Ulimits"] == [{"Name": "nofile", "Soft": 1024, "Hard": 2048}]


def test_spec_to_config_never_privileged() -> None:
    """No public field on ContainerSpec exposes Privileged; assert the final
    HostConfig never contains it either (paranoia check that no helper
    silently enables privileged mode)."""
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert "Privileged" not in hc or hc["Privileged"] is False
