"""Tests for rolemesh.agent.executor -- data types and backend configs."""

from __future__ import annotations

from rolemesh.agent.executor import (
    CLAUDE_CODE_BACKEND,
    PIMONO_BACKEND,
    AgentBackendConfig,
    AgentInput,
    AgentOutput,
)
from rolemesh.auth.permissions import AgentPermissions


def test_agent_input_frozen() -> None:
    perms = AgentPermissions.for_role("super_agent").to_dict()
    inp = AgentInput(prompt="hello", group_folder="g", chat_jid="j", permissions=perms)
    try:
        inp.prompt = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_agent_input_optional_fields() -> None:
    perms = AgentPermissions.for_role("agent").to_dict()
    inp = AgentInput(prompt="p", group_folder="g", chat_jid="j", permissions=perms)
    assert inp.session_id is None
    assert inp.is_scheduled_task is False
    assert inp.assistant_name is None
    assert inp.system_prompt is None
    assert inp.role_config is None
    assert inp.user_id == ""


def test_agent_input_all_fields() -> None:
    perms = AgentPermissions.for_role("super_agent").to_dict()
    inp = AgentInput(
        prompt="hello",
        group_folder="grp",
        chat_jid="jid",
        permissions=perms,
        user_id="user-1",
        session_id="s1",
        is_scheduled_task=True,
        assistant_name="Andy",
        system_prompt="You are helpful",
        role_config={"role": "coder"},
    )
    assert inp.prompt == "hello"
    assert inp.permissions["data_scope"] == "tenant"
    assert inp.user_id == "user-1"
    assert inp.system_prompt == "You are helpful"
    assert inp.role_config == {"role": "coder"}


def test_agent_output_frozen() -> None:
    out = AgentOutput(status="success", result="done")
    try:
        out.status = "error"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_agent_output_optional_fields() -> None:
    out = AgentOutput(status="success", result=None)
    assert out.new_session_id is None
    assert out.error is None


def test_agent_backend_config_defaults() -> None:
    cfg = AgentBackendConfig(name="test", image="test:latest")
    assert cfg.entrypoint is None
    assert cfg.extra_mounts == []
    assert cfg.extra_env == {}
    assert cfg.skip_claude_session is False


def test_claude_code_backend_preset() -> None:
    assert CLAUDE_CODE_BACKEND.name == "claude-code"
    assert CLAUDE_CODE_BACKEND.image == "rolemesh-agent:latest"
    assert CLAUDE_CODE_BACKEND.entrypoint is None
    assert CLAUDE_CODE_BACKEND.skip_claude_session is False


def test_pimono_backend_preset() -> None:
    assert PIMONO_BACKEND.name == "pi-mono"
    assert PIMONO_BACKEND.image == "ppi-agent:latest"
    assert PIMONO_BACKEND.entrypoint == ["python", "-m", "ppi.coding_agent", "--mode", "rolemesh"]
    assert PIMONO_BACKEND.skip_claude_session is True


def test_agent_backend_config_frozen() -> None:
    cfg = AgentBackendConfig(name="t", image="i")
    try:
        cfg.name = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass
