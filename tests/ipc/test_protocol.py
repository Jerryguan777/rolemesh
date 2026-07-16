"""Tests for rolemesh.ipc.protocol -- IPC message serialization."""

from __future__ import annotations

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.ipc.protocol import AgentInitData


def test_agent_init_data_roundtrip() -> None:
    """AgentInitData serializes and deserializes correctly."""
    perms = AgentPermissions(
        task_schedule=True, task_manage_others=True, agent_delegate=True
    ).to_dict()
    init = AgentInitData(
        prompt="Hello world",
        group_folder="mygroup",
        chat_jid="tg:12345",
        permissions=perms,
        user_id="user-1",
        session_id="sess-001",
        is_scheduled_task=False,
        assistant_name="Andy",
        system_prompt="Be helpful",
        role_config={"role": "coder"},
    )
    data = init.serialize()
    restored = AgentInitData.deserialize(data)
    assert restored.prompt == init.prompt
    assert restored.group_folder == init.group_folder
    assert restored.chat_jid == init.chat_jid
    assert restored.permissions == init.permissions
    assert restored.user_id == init.user_id
    assert restored.session_id == init.session_id
    assert restored.is_scheduled_task == init.is_scheduled_task
    assert restored.assistant_name == init.assistant_name
    assert restored.system_prompt == init.system_prompt
    assert restored.role_config == init.role_config


def test_agent_init_data_optional_fields() -> None:
    """AgentInitData handles missing optional fields."""
    perms = AgentPermissions().to_dict()
    init = AgentInitData(
        prompt="Test",
        group_folder="group",
        chat_jid="jid",
        permissions=perms,
    )
    data = init.serialize()
    restored = AgentInitData.deserialize(data)
    assert restored.session_id is None
    assert restored.is_scheduled_task is False
    assert restored.assistant_name is None
    assert restored.system_prompt is None
    assert restored.role_config is None
    assert restored.user_id == ""
    assert restored.run_id is None


def test_agent_init_data_run_id_roundtrip() -> None:
    """Run attribution seed (single-writer refactor) survives the
    KV wire, and its absence — an orchestrator predating the field —
    deserializes to None rather than erroring."""
    perms = AgentPermissions().to_dict()
    init = AgentInitData(
        prompt="Test",
        group_folder="group",
        chat_jid="jid",
        permissions=perms,
        run_id="run-abc",
    )
    restored = AgentInitData.deserialize(init.serialize())
    assert restored.run_id == "run-abc"

    # Payload from an older orchestrator: no run_id key at all.
    import json

    raw = json.loads(init.serialize())
    del raw["run_id"]
    legacy = AgentInitData.deserialize(json.dumps(raw).encode())
    assert legacy.run_id is None


def test_agent_init_data_frozen() -> None:
    """AgentInitData is immutable."""
    init = AgentInitData(
        prompt="p",
        group_folder="g",
        chat_jid="j",
        permissions=AgentPermissions(task_manage_others=True).to_dict(),
    )
    try:
        init.prompt = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_agent_init_data_missing_permissions_defaults_to_least_privilege() -> None:
    """Missing/empty permissions in raw JSON coerce to all-False defaults."""
    import json

    raw = json.dumps({
        "prompt": "test",
        "group_folder": "g",
        "chat_jid": "j",
    }).encode()
    restored = AgentInitData.deserialize(raw)
    assert restored.permissions["task_schedule"] is False
    assert restored.permissions["task_manage_others"] is False
    assert restored.permissions["agent_delegate"] is False
