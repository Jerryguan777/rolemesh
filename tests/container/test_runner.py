"""Tests for rolemesh.container.runner -- pure functions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from rolemesh.agent.executor import AgentBackendConfig
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.container.runner import (
    AvailableGroup,
    ContainerInput,
    ContainerOutput,
    _clamp_cpu,
    _clamp_memory,
    _filter_env_allowlist,
    build_container_spec,
    build_volume_mounts,
)
from rolemesh.container.runtime import VolumeMount
from rolemesh.core.types import ContainerConfig, Coworker

if TYPE_CHECKING:
    from pathlib import Path


def _make_coworker(folder: str = "test-group", agent_role: str = "agent") -> Coworker:
    return Coworker(
        id="cw-1",
        tenant_id="t-1",
        name="Test Coworker",
        folder=folder,
        agent_role=agent_role,
    )


class TestBuildVolumeMounts:
    def test_agent_has_group_folder(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        tenant_dir = tmp_path / "tenants" / "t-1" / "coworkers" / coworker.folder
        tenant_dir.mkdir(parents=True)
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", permissions=AgentPermissions.for_role("agent"))
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/group" in container_paths

    def test_super_agent_has_project_and_group(self, tmp_path: Path) -> None:
        coworker = _make_coworker(agent_role="super_agent")
        tenant_dir = tmp_path / "tenants" / "t-1" / "coworkers" / coworker.folder
        tenant_dir.mkdir(parents=True)
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(
                coworker, "t-1", "conv-1", permissions=AgentPermissions.for_role("super_agent")
            )
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/project" in container_paths
        assert "/workspace/group" in container_paths

    def test_backend_config_skip_claude_session(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        config = AgentBackendConfig(name="test", image="test:latest", skip_claude_session=True)
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(
                coworker, "t-1", "conv-1", permissions=AgentPermissions.for_role("agent"), backend_config=config
            )
        container_paths = [m.container_path for m in mounts]
        assert not any(".claude" in p for p in container_paths)

    def test_backend_config_extra_mounts(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        config = AgentBackendConfig(
            name="test",
            image="test:latest",
            extra_mounts=[("/extra/host", "/extra/container", True)],
        )
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(
                coworker, "t-1", "conv-1", permissions=AgentPermissions.for_role("agent"), backend_config=config
            )
        container_paths = [m.container_path for m in mounts]
        assert "/extra/container" in container_paths

    def test_session_and_logs_dirs_created(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", permissions=AgentPermissions.for_role("agent"))
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/sessions" in container_paths
        assert "/workspace/logs" in container_paths

    def test_shared_dir_mounted_if_exists(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        shared = tmp_path / "tenants" / "t-1" / "shared"
        shared.mkdir(parents=True)
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", permissions=AgentPermissions.for_role("agent"))
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/shared" in container_paths

    def test_legacy_is_main_param_still_works(self, tmp_path: Path) -> None:
        """Legacy is_main=True param should produce project mount (backward compat)."""
        coworker = _make_coworker()
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=True)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/project" in container_paths


class TestBuildContainerSpec:
    def test_basic_spec(self) -> None:
        mounts = [VolumeMount(host_path="/a", container_path="/b", readonly=True)]
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec(mounts, "test-container", "job-123")
        assert spec.name == "test-container"
        assert spec.image == "rolemesh-agent:latest"
        assert "JOB_ID" in spec.env
        assert spec.env["JOB_ID"] == "job-123"
        assert "ANTHROPIC_API_KEY" in spec.env

    def test_spec_with_backend_config(self) -> None:
        mounts = [VolumeMount(host_path="/a", container_path="/b", readonly=False)]
        config = AgentBackendConfig(
            name="pi-mono",
            image="ppi:latest",
            entrypoint=["python", "-m", "ppi"],
            extra_env={"PI_MODEL_ID": "claude-opus-4-7"},
        )
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="oauth"):
            spec = build_container_spec(mounts, "test-ppi", "job-456", backend_config=config)
        assert spec.image == "ppi:latest"
        assert spec.entrypoint == ["python", "-m", "ppi"]
        assert spec.env["PI_MODEL_ID"] == "claude-opus-4-7"
        assert "CLAUDE_CODE_OAUTH_TOKEN" in spec.env

    def test_spec_env_has_nats_url(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert "NATS_URL" in spec.env
        assert "host.docker.internal" in spec.env["NATS_URL"]


class TestBackwardCompatAliases:
    def test_container_input_is_agent_input(self) -> None:
        from rolemesh.agent.executor import AgentInput

        assert ContainerInput is AgentInput

    def test_container_output_is_agent_output(self) -> None:
        from rolemesh.agent.executor import AgentOutput

        assert ContainerOutput is AgentOutput


class TestAvailableGroup:
    def test_available_group_frozen(self) -> None:
        g = AvailableGroup(jid="j", name="n", last_activity="2024-01-01", is_registered=True)
        try:
            g.jid = "other"  # type: ignore[misc]
            raise AssertionError("Should have raised")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# R7: resource-limit merge + clamping
# ---------------------------------------------------------------------------


class TestResourceLimitMerge:
    def test_global_default_applied_when_no_coworker_override(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        # Global default CONTAINER_MEMORY_LIMIT = "2g"
        assert spec.memory_limit == "2g"
        assert spec.cpu_limit == 2.0

    def test_coworker_override_wins_over_global(self) -> None:
        cw = Coworker(
            id="cw-1", tenant_id="t-1", name="Test", folder="f",
            container_config=ContainerConfig(memory_limit="1g", cpu_limit=1.0),
        )
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j", coworker=cw)
        assert spec.memory_limit == "1g"
        assert spec.cpu_limit == 1.0

    def test_coworker_override_exceeds_max_gets_clamped(self) -> None:
        cw = Coworker(
            id="cw-1", tenant_id="t-1", name="Greedy", folder="f",
            container_config=ContainerConfig(memory_limit="64g", cpu_limit=32.0),
        )
        # Capture structured warnings by mocking the module logger directly.
        # (structlog's PrintLoggerFactory holds a reference to sys.stderr taken
        # at import time, so pytest capsys/capfd don't see it.)
        with (
            patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"),
            patch("rolemesh.container.runner.logger") as mock_logger,
        ):
            spec = build_container_spec([], "c", "j", coworker=cw)
        assert spec.memory_limit == "8g"
        assert spec.cpu_limit == 4.0
        # Both memory and cpu clamps should have logged structured warnings.
        warning_calls = mock_logger.warning.call_args_list
        kwargs_list = [c.kwargs for c in warning_calls]
        assert any(kw.get("coworker") == "Greedy" and "memory" in (c.args[0] if c.args else "").lower()
                   for c, kw in zip(warning_calls, kwargs_list, strict=True))
        assert any(kw.get("coworker") == "Greedy" and "cpu" in (c.args[0] if c.args else "").lower()
                   for c, kw in zip(warning_calls, kwargs_list, strict=True))

    def test_no_swap_and_pids_limit_defaults_on_spec(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert spec.pids_limit == 512
        assert spec.memory_swappiness == 0
        # memory_swap left None at the spec layer; docker_runtime sets it == Memory.

    def test_readonly_rootfs_and_tmpfs_defaulted(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert spec.readonly_rootfs is True
        assert "/tmp" in spec.tmpfs
        assert "/home/agent/.cache" in spec.tmpfs

    def test_security_opt_contains_no_new_privileges(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert "no-new-privileges:true" in spec.security_opt


class TestClampHelpers:
    def test_clamp_memory_returns_value_when_under_cap(self) -> None:
        assert _clamp_memory("1g", "8g", coworker_name="x") == "1g"

    def test_clamp_memory_clamps_when_over_cap(self) -> None:
        assert _clamp_memory("16g", "8g", coworker_name="x") == "8g"

    def test_clamp_memory_boundary_equal_is_not_clamped(self) -> None:
        assert _clamp_memory("8g", "8g", coworker_name="x") == "8g"

    def test_clamp_cpu_clamps_over_cap(self) -> None:
        assert _clamp_cpu(16.0, 4.0, coworker_name="x") == 4.0

    def test_clamp_cpu_keeps_under_cap(self) -> None:
        assert _clamp_cpu(1.5, 4.0, coworker_name="x") == 1.5


# ---------------------------------------------------------------------------
# R8: env allowlist
# ---------------------------------------------------------------------------


class TestEnvAllowlist:
    def test_allowlisted_keys_pass_through(self) -> None:
        out = _filter_env_allowlist({"TZ": "UTC", "NATS_URL": "x"}, source="test")
        assert out == {"TZ": "UTC", "NATS_URL": "x"}

    def test_unknown_key_is_dropped(self) -> None:
        out = _filter_env_allowlist({"TZ": "UTC", "SECRET_TOKEN": "s3cret"}, source="test")
        assert "SECRET_TOKEN" not in out
        assert out["TZ"] == "UTC"

    def test_rejection_log_contains_key_name_not_value(self) -> None:
        with patch("rolemesh.container.runner.logger") as mock_logger:
            _filter_env_allowlist({"MY_SECRET": "abc123xyz"}, source="test")
        # Exactly one warning: dropped key list, no values anywhere in args/kwargs.
        mock_logger.warning.assert_called_once()
        call = mock_logger.warning.call_args
        serialized = repr((call.args, call.kwargs))
        assert "MY_SECRET" in serialized
        assert "abc123xyz" not in serialized

    def test_backend_extra_env_unknown_key_dropped(self) -> None:
        config = AgentBackendConfig(
            name="t", image="i",
            extra_env={"AGENT_BACKEND": "pi", "RANDOM_LEAK": "should-be-dropped"},
        )
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j", backend_config=config)
        assert "AGENT_BACKEND" in spec.env
        assert "RANDOM_LEAK" not in spec.env

    def test_spec_env_contains_only_allowlisted_keys(self) -> None:
        from rolemesh.core.config import CONTAINER_ENV_ALLOWLIST
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert set(spec.env.keys()) <= CONTAINER_ENV_ALLOWLIST


# ---------------------------------------------------------------------------
# R5: metadata blackhole + custom network
# ---------------------------------------------------------------------------


class TestMetadataBlackholeAndNetwork:
    def test_metadata_blackhole_present_in_extra_hosts(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert spec.extra_hosts.get("169.254.169.254") == "127.0.0.1"
        assert spec.extra_hosts.get("metadata.google.internal") == "127.0.0.1"

    def test_custom_network_name_applied_from_config(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        # Default config points at rolemesh-agent-net unless CONTAINER_NETWORK_NAME='' is set.
        assert spec.network_name == "rolemesh-agent-net"

    def test_empty_network_name_yields_none(self) -> None:
        """Operator escape hatch: CONTAINER_NETWORK_NAME='' -> None -> Docker default bridge."""
        with (
            patch("rolemesh.container.runner.CONTAINER_NETWORK_NAME", ""),
            patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"),
        ):
            spec = build_container_spec([], "c", "j")
        assert spec.network_name is None


# ---------------------------------------------------------------------------
# R1: OCI runtime merge
# ---------------------------------------------------------------------------


class TestOciRuntimeMerge:
    def test_global_default_runc(self) -> None:
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"):
            spec = build_container_spec([], "c", "j")
        assert spec.runtime == "runc"

    def test_global_default_runsc(self) -> None:
        with (
            patch("rolemesh.container.runner.CONTAINER_RUNTIME", "runsc"),
            patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"),
        ):
            spec = build_container_spec([], "c", "j")
        assert spec.runtime == "runsc"

    def test_coworker_override_wins(self) -> None:
        """A coworker incompatible with gVisor can downgrade to runc."""
        cw = Coworker(
            id="cw-1", tenant_id="t-1", name="LegacyTools", folder="f",
            container_config=ContainerConfig(runtime="runc"),
        )
        with (
            patch("rolemesh.container.runner.CONTAINER_RUNTIME", "runsc"),
            patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"),
        ):
            spec = build_container_spec([], "c", "j", coworker=cw)
        assert spec.runtime == "runc"

    def test_coworker_inherits_global_when_unset(self) -> None:
        cw = Coworker(
            id="cw-1", tenant_id="t-1", name="Neutral", folder="f",
            container_config=ContainerConfig(runtime=None),
        )
        with (
            patch("rolemesh.container.runner.CONTAINER_RUNTIME", "runsc"),
            patch("rolemesh.container.runner.detect_auth_mode", return_value="api-key"),
        ):
            spec = build_container_spec([], "c", "j", coworker=cw)
        assert spec.runtime == "runsc"
