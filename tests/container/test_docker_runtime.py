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
    mock_docker.version = AsyncMock(return_value={"Version": "24.0.7"})
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


def test_spec_to_config_custom_network() -> None:
    spec = ContainerSpec(name="t", image="i", network_name="rolemesh-agent-net")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["NetworkMode"] == "rolemesh-agent-net"


def test_spec_to_config_no_network_name_leaves_docker_default() -> None:
    spec = ContainerSpec(name="t", image="i")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    # Absence of NetworkMode means Docker default bridge is used.
    assert "NetworkMode" not in hc


# ---------------------------------------------------------------------------
# R1: OCI runtime selection
# ---------------------------------------------------------------------------


def test_spec_to_config_runtime_runc() -> None:
    spec = ContainerSpec(name="t", image="i", runtime="runc")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["Runtime"] == "runc"


def test_spec_to_config_runtime_runsc() -> None:
    spec = ContainerSpec(name="t", image="i", runtime="runsc")
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert hc["Runtime"] == "runsc"


def test_spec_to_config_runtime_absent_when_none() -> None:
    """runtime=None must leave HostConfig.Runtime unset so Docker picks
    its default — existing deployments without gVisor registered must
    keep working."""
    spec = ContainerSpec(name="t", image="i", runtime=None)
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert "Runtime" not in hc


# ---------------------------------------------------------------------------
# R6: docker.sock bind blockade
# ---------------------------------------------------------------------------


def test_no_docker_socket_ever_mounted_direct() -> None:
    """A spec with a docker.sock bind must be rejected before serialization."""
    spec = ContainerSpec(
        name="t", image="i",
        mounts=[VolumeMount(
            host_path="/var/run/docker.sock",
            container_path="/var/run/docker.sock",
            readonly=True,
        )],
    )
    with pytest.raises(ValueError, match="docker socket"):
        DockerRuntime._spec_to_config(spec)


def test_no_docker_socket_ever_mounted_container_path_only() -> None:
    """Defense-in-depth: match on any segment, not just host path."""
    spec = ContainerSpec(
        name="t", image="i",
        mounts=[VolumeMount(
            host_path="/tmp/innocent-looking",
            container_path="/var/run/docker.sock",
            readonly=False,
        )],
    )
    with pytest.raises(ValueError, match="docker socket"):
        DockerRuntime._spec_to_config(spec)


def test_docker_socket_blockade_applies_to_full_pipeline() -> None:
    """Verifies the check runs inside the normal _spec_to_config path, not
    only in some unused helper."""
    from rolemesh.container.runner import build_container_spec

    mounts = [VolumeMount(host_path="/var/run/docker.sock", container_path="/sock", readonly=True)]
    with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
        spec = build_container_spec(mounts, "c", "j")
    with pytest.raises(ValueError, match="docker socket"):
        DockerRuntime._spec_to_config(spec)


# ---------------------------------------------------------------------------
# docker.sock detection — regression for substring false positive/negative
# Before the basename-based check, `"docker.sock" in path` collided with
# legitimate paths like /tmp/docker.socket-tests (substring hit inside
# "docker.socket") and blocked valid mounts. The matrix below pins the
# new semantics so future refactors can't silently reintroduce the bug.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/var/run/docker.sock",           # canonical path
    "/run/docker.sock",                # rootless docker layout
    "/some/nested/path/docker.sock",   # anywhere in tree
    "/var/run/docker.sock/",           # trailing slash — still the same file
])
def test_is_docker_socket_path_rejects_real_socket(path: str) -> None:
    from rolemesh.container.docker_runtime import _is_docker_socket_path
    assert _is_docker_socket_path(path) is True


@pytest.mark.parametrize("path", [
    "/tmp/docker.socket-tests/foo",    # regression: .socket contains .sock as substring
    "/home/agent/docker.socks.log",    # regression: .socks contains .sock
    "/home/agent/mydocker.sock.bak",   # basename is mydocker.sock.bak, not docker.sock
    "/workspace/my-docker.sock",       # different basename
    "/var/run/docker-sock",            # hyphen instead of dot
    "/dev/null",                       # unrelated
    "",                                 # empty
])
def test_is_docker_socket_path_allows_legitimate_paths(path: str) -> None:
    from rolemesh.container.docker_runtime import _is_docker_socket_path
    assert _is_docker_socket_path(path) is False


def test_mount_blockade_rejects_host_side_socket() -> None:
    spec = ContainerSpec(
        name="t", image="i",
        mounts=[VolumeMount(
            host_path="/var/run/docker.sock",
            container_path="/safe/path",
            readonly=True,
        )],
    )
    with pytest.raises(ValueError, match="docker socket"):
        DockerRuntime._spec_to_config(spec)


def test_mount_blockade_rejects_container_side_socket() -> None:
    """Even if the host path is innocuous, binding it TO docker.sock inside
    the container is a misconfiguration the guard must still catch."""
    spec = ContainerSpec(
        name="t", image="i",
        mounts=[VolumeMount(
            host_path="/tmp/anything",
            container_path="/var/run/docker.sock",
            readonly=False,
        )],
    )
    with pytest.raises(ValueError, match="docker socket"):
        DockerRuntime._spec_to_config(spec)


def test_mount_blockade_does_not_false_positive_on_docker_socket_dir() -> None:
    """A directory named docker.socket-tests in the path must not trip the guard."""
    spec = ContainerSpec(
        name="t", image="i",
        mounts=[VolumeMount(
            host_path="/tmp/docker.socket-tests/fixtures",
            container_path="/work/fixtures",
            readonly=True,
        )],
    )
    # Must not raise.
    hc = DockerRuntime._spec_to_config(spec)["HostConfig"]
    assert any("docker.socket-tests" in b for b in hc["Binds"])


# ---------------------------------------------------------------------------
# R5.1-1: dockerd version check
# ---------------------------------------------------------------------------


async def test_check_daemon_version_rejects_old_docker() -> None:
    from rolemesh.container.docker_runtime import IncompatibleDockerVersionError

    rt = DockerRuntime()
    mock_client = MagicMock()
    mock_client.version = AsyncMock(return_value={"Version": "19.3.14"})
    rt._client = mock_client

    with pytest.raises(IncompatibleDockerVersionError, match="below the hardening floor"):
        await rt._check_daemon_version()


async def test_check_daemon_version_accepts_floor() -> None:
    rt = DockerRuntime()
    mock_client = MagicMock()
    mock_client.version = AsyncMock(return_value={"Version": "20.10.0"})
    rt._client = mock_client

    await rt._check_daemon_version()  # must not raise


async def test_check_daemon_version_accepts_new_docker() -> None:
    rt = DockerRuntime()
    mock_client = MagicMock()
    mock_client.version = AsyncMock(return_value={"Version": "24.0.7"})
    rt._client = mock_client

    await rt._check_daemon_version()


async def test_check_daemon_version_unparseable_falls_through() -> None:
    """Don't fail-closed on a garbage version string — log and proceed."""
    rt = DockerRuntime()
    mock_client = MagicMock()
    mock_client.version = AsyncMock(return_value={"Version": "canary-build"})
    rt._client = mock_client

    await rt._check_daemon_version()  # must not raise
