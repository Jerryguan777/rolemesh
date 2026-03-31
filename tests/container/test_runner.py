"""Tests for rolemesh.container.runner -- pure functions."""

from __future__ import annotations

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
from rolemesh.core.types import RegisteredGroup


def _make_group(folder: str = "test-group", is_main: bool = False) -> RegisteredGroup:
    return RegisteredGroup(
        name="Test Group",
        folder=folder,
        trigger="@Andy",
        added_at="2024-01-01",
        is_main=is_main,
    )


class TestBuildVolumeMounts:
    def test_non_main_has_group_folder(self, tmp_path: object) -> None:
        group = _make_group()
        with (
            patch("rolemesh.container.runner.resolve_group_folder_path", return_value=tmp_path),
            patch("rolemesh.container.runner.GROUPS_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(group, is_main=False)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/group" in container_paths

    def test_main_has_project_and_group(self, tmp_path: object) -> None:
        group = _make_group(is_main=True)
        with (
            patch("rolemesh.container.runner.resolve_group_folder_path", return_value=tmp_path),
            patch("rolemesh.container.runner.GROUPS_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(group, is_main=True)
        container_paths = [m.container_path for m in mounts]
        assert "/workspace/project" in container_paths
        assert "/workspace/group" in container_paths

    def test_backend_config_skip_claude_session(self, tmp_path: object) -> None:
        group = _make_group()
        config = AgentBackendConfig(name="test", image="test:latest", skip_claude_session=True)
        with (
            patch("rolemesh.container.runner.resolve_group_folder_path", return_value=tmp_path),
            patch("rolemesh.container.runner.GROUPS_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(group, is_main=False, backend_config=config)
        container_paths = [m.container_path for m in mounts]
        assert not any(".claude" in p for p in container_paths)

    def test_backend_config_extra_mounts(self, tmp_path: object) -> None:
        group = _make_group()
        config = AgentBackendConfig(
            name="test",
            image="test:latest",
            extra_mounts=[("/extra/host", "/extra/container", True)],
        )
        with (
            patch("rolemesh.container.runner.resolve_group_folder_path", return_value=tmp_path),
            patch("rolemesh.container.runner.GROUPS_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(group, is_main=False, backend_config=config)
        container_paths = [m.container_path for m in mounts]
        assert "/extra/container" in container_paths


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
