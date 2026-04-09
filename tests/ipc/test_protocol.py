"""Tests for rolemesh.ipc.protocol -- IPC message serialization."""

from __future__ import annotations

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.ipc.protocol import AgentInitData


def test_agent_init_data_roundtrip() -> None:
    """AgentInitData serializes and deserializes correctly."""
    perms = AgentPermissions.for_role("super_agent").to_dict()
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
    perms = AgentPermissions.for_role("agent").to_dict()
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


def test_agent_init_data_frozen() -> None:
    """AgentInitData is immutable."""
    init = AgentInitData(
        prompt="p",
        group_folder="g",
        chat_jid="j",
        permissions=AgentPermissions.for_role("super_agent").to_dict(),
    )
    try:
        init.prompt = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass


def test_agent_init_data_backward_compat_is_main_true() -> None:
    """Legacy is_main=True in raw JSON converts to super_agent permissions."""
    import json

    raw = json.dumps({
        "prompt": "test",
        "group_folder": "g",
        "chat_jid": "j",
        "is_main": True,
        "tenant_id": "t1",
    }).encode()
    restored = AgentInitData.deserialize(raw)
    assert restored.permissions["data_scope"] == "tenant"
    assert restored.permissions["task_schedule"] is True
    assert restored.permissions["task_manage_others"] is True
    assert restored.permissions["agent_delegate"] is True


def test_agent_init_data_backward_compat_is_main_false() -> None:
    """Legacy is_main=False in raw JSON converts to agent permissions."""
    import json

    raw = json.dumps({
        "prompt": "test",
        "group_folder": "g",
        "chat_jid": "j",
        "is_main": False,
    }).encode()
    restored = AgentInitData.deserialize(raw)
    assert restored.permissions["data_scope"] == "self"
    assert restored.permissions["task_schedule"] is False
