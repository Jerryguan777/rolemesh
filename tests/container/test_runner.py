"""Tests for rolemesh.container.runner -- pure functions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from rolemesh.agent.executor import AgentBackendConfig
from rolemesh.container.runner import (
    AvailableGroup,
    ContainerInput,
    ContainerOutput,
    build_container_spec,
    build_volume_mounts,
)
from rolemesh.container.runtime import VolumeMount
from rolemesh.core.types import Coworker

if TYPE_CHECKING:
    from pathlib import Path


def _make_coworker(folder: str = "test-group", is_admin: bool = False) -> Coworker:
    return Coworker(
        id="cw-1",
        tenant_id="t-1",
        name="Test Coworker",
        folder=folder,
        is_admin=is_admin,
    )


class TestBuildVolumeMounts:
    def test_non_main_has_group_folder(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        tenant_dir = tmp_path / "tenants" / "t-1" / "coworkers" / coworker.folder
        tenant_dir.mkdir(parents=True)
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=False)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/group" in container_paths

    def test_main_has_project_and_group(self, tmp_path: Path) -> None:
        coworker = _make_coworker(is_admin=True)
        tenant_dir = tmp_path / "tenants" / "t-1" / "coworkers" / coworker.folder
        tenant_dir.mkdir(parents=True)
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=True)
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
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=False, backend_config=config)
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
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=False, backend_config=config)
        container_paths = [m.container_path for m in mounts]
        assert "/extra/container" in container_paths

    def test_session_and_logs_dirs_created(self, tmp_path: Path) -> None:
        coworker = _make_coworker()
        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=False)
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
            mounts = build_volume_mounts(coworker, "t-1", "conv-1", is_main=False)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/shared" in container_paths


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
            extra_env={"CUSTOM_VAR": "value"},
        )
        with patch("rolemesh.container.runner.detect_auth_mode", return_value="oauth"):
            spec = build_container_spec(mounts, "test-ppi", "job-456", backend_config=config)
        assert spec.image == "ppi:latest"
        assert spec.entrypoint == ["python", "-m", "ppi"]
        assert spec.env["CUSTOM_VAR"] == "value"
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
