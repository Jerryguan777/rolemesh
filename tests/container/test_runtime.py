"""Tests for rolemesh.container.runtime -- Protocol types and factory."""

from __future__ import annotations

import pytest

from rolemesh.container.runtime import (
    CONTAINER_HOST_GATEWAY,
    ContainerSpec,
    VolumeMount,
    get_host_gateway_extra_hosts,
    get_runtime,
    rewrite_loopback_to_host_gateway,
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
    with pytest.raises(ValueError, match="Unknown container backend"):
        get_runtime("podman")


# ---------------------------------------------------------------------------
# rewrite_loopback_to_host_gateway — universal loopback rewrite
# ---------------------------------------------------------------------------


class TestRewriteLoopbackToHostGateway:
    """``rewrite_loopback_to_host_gateway`` is shared by every IPC
    boundary that hands a URL into a container (NATS_URL on gateway
    spawn; MCP server origins serialised into NATS events). These
    cases pin the contract from both sides — what must rewrite, what
    must NOT — so a future revert to a platform-gated implementation
    surfaces here rather than as a runtime "Connection refused" only
    on macOS.
    """

    def test_localhost_in_authority_is_rewritten(self) -> None:
        assert rewrite_loopback_to_host_gateway("nats://localhost:4222") == (
            "nats://host.docker.internal:4222"
        )

    def test_127_0_0_1_is_rewritten(self) -> None:
        assert rewrite_loopback_to_host_gateway("nats://127.0.0.1:4222") == (
            "nats://host.docker.internal:4222"
        )

    def test_already_host_docker_internal_is_idempotent(self) -> None:
        # Belt-and-braces: if a deploy already crafted the right URL
        # (e.g. NATS_URL env override), running the rewrite a second
        # time must not corrupt it.
        assert rewrite_loopback_to_host_gateway(
            "nats://host.docker.internal:4222"
        ) == "nats://host.docker.internal:4222"

    def test_external_hostname_is_left_alone(self) -> None:
        # An operator pointing at a remote NATS / MCP cluster should
        # NOT see their hostname mangled. Anchoring on ``://...:``
        # guards against ``mylocalhost.example.com`` style bystanders.
        assert rewrite_loopback_to_host_gateway(
            "nats://nats.cluster.internal:4222"
        ) == "nats://nats.cluster.internal:4222"

    def test_substring_localhost_in_path_is_left_alone(self) -> None:
        # Path-side ``localhost`` must NOT be rewritten — the rewrite
        # is anchored on ``://localhost:`` (port-colon required).
        assert rewrite_loopback_to_host_gateway(
            "https://nats.example.com/path/localhost/x"
        ) == "https://nats.example.com/path/localhost/x"

    def test_handles_https_scheme(self) -> None:
        # MCP origins typically arrive as https://localhost:8509.
        # Bug 5 was specifically that this URL was passed verbatim
        # through ``egress.mcp.changed`` and the gateway dialed itself.
        assert rewrite_loopback_to_host_gateway(
            "https://localhost:8509"
        ) == "https://host.docker.internal:8509"

    def test_handles_http_scheme(self) -> None:
        assert rewrite_loopback_to_host_gateway(
            "http://127.0.0.1:9100/mcp/"
        ) == "http://host.docker.internal:9100/mcp/"

    def test_no_loopback_no_change(self) -> None:
        # Sanity: clean URLs passed through unchanged.
        assert rewrite_loopback_to_host_gateway(
            "https://api.github.com"
        ) == "https://api.github.com"
