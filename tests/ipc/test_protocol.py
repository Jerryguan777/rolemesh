"""Tests for rolemesh.ipc.protocol -- IPC message serialization."""

from __future__ import annotations

from rolemesh.ipc.protocol import AgentInitData


def test_agent_init_data_roundtrip() -> None:
    """AgentInitData serializes and deserializes correctly."""
    init = AgentInitData(
        prompt="Hello world",
        group_folder="mygroup",
        chat_jid="tg:12345",
        is_main=True,
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
    assert restored.is_main == init.is_main
    assert restored.session_id == init.session_id
    assert restored.is_scheduled_task == init.is_scheduled_task
    assert restored.assistant_name == init.assistant_name
    assert restored.system_prompt == init.system_prompt
    assert restored.role_config == init.role_config


def test_agent_init_data_optional_fields() -> None:
    """AgentInitData handles missing optional fields."""
    init = AgentInitData(
        prompt="Test",
        group_folder="group",
        chat_jid="jid",
        is_main=False,
    )
    data = init.serialize()
    restored = AgentInitData.deserialize(data)
    assert restored.session_id is None
    assert restored.is_scheduled_task is False
    assert restored.assistant_name is None
    assert restored.system_prompt is None
    assert restored.role_config is None


def test_agent_init_data_frozen() -> None:
    """AgentInitData is immutable."""
    init = AgentInitData(
        prompt="p",
        group_folder="g",
        chat_jid="j",
        is_main=True,
    )
    try:
        init.prompt = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised")
    except AttributeError:
        pass
