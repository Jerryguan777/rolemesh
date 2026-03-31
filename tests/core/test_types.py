"""Tests for rolemesh.types."""

from rolemesh.core.types import (
    AdditionalMount,
    ChannelBinding,
    ContainerConfig,
    Conversation,
    Coworker,
    NewMessage,
    RegisteredGroup,
    ScheduledTask,
    TaskRunLog,
    Tenant,
    User,
    registered_group_to_coworker,
)


def test_additional_mount_defaults() -> None:
    mount = AdditionalMount(host_path="/tmp/test")
    assert mount.host_path == "/tmp/test"
    assert mount.container_path is None
    assert mount.readonly is True


def test_container_config_defaults() -> None:
    cfg = ContainerConfig()
    assert cfg.additional_mounts == []
    assert cfg.timeout == 300_000


def test_registered_group() -> None:
    group = RegisteredGroup(name="test", folder="test", trigger="@Andy", added_at="2024-01-01")
    assert group.requires_trigger is True
    assert group.is_main is False
    assert group.container_config is None


def test_new_message() -> None:
    msg = NewMessage(
        id="1",
        chat_jid="chat@jid",
        sender="user@jid",
        sender_name="User",
        content="Hello",
        timestamp="2024-01-01T00:00:00Z",
    )
    assert msg.is_from_me is False
    assert msg.is_bot_message is False


def test_scheduled_task_defaults() -> None:
    task = ScheduledTask(
        id="t1",
        tenant_id="tenant-1",
        coworker_id="cw-1",
        prompt="Do something",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
    )
    assert task.status == "active"
    assert task.next_run is None
    assert task.conversation_id is None


def test_task_run_log() -> None:
    log = TaskRunLog(
        tenant_id="t1",
        task_id="t1",
        run_at="2024-01-01T00:00:00Z",
        duration_ms=1000,
        status="success",
    )
    assert log.result is None
    assert log.error is None


def test_tenant_defaults() -> None:
    t = Tenant(id="t1", name="Test")
    assert t.slug is None
    assert t.max_concurrent_containers == 5
    assert t.last_message_cursor is None


def test_coworker_defaults() -> None:
    cw = Coworker(id="cw1", tenant_id="t1", name="Bot", folder="bot")
    assert cw.agent_backend == "claude-code"
    assert cw.tools == []
    assert cw.skills == []
    assert cw.is_admin is False
    assert cw.max_concurrent == 2
    assert cw.status == "active"


def test_channel_binding_defaults() -> None:
    cb = ChannelBinding(id="cb1", coworker_id="cw1", tenant_id="t1", channel_type="telegram")
    assert cb.credentials == {}
    assert cb.status == "active"


def test_conversation_defaults() -> None:
    c = Conversation(
        id="c1",
        tenant_id="t1",
        coworker_id="cw1",
        channel_binding_id="cb1",
        channel_chat_id="12345",
    )
    assert c.requires_trigger is True
    assert c.last_agent_invocation is None


def test_user_defaults() -> None:
    u = User(id="u1", tenant_id="t1", name="Test")
    assert u.role == "member"
    assert u.channel_ids == {}


def test_registered_group_to_coworker() -> None:
    group = RegisteredGroup(name="test", folder="test", trigger="@Andy", added_at="2024-01-01", is_main=True)
    cw = registered_group_to_coworker(group, "t1", "cw1")
    assert cw.name == "test"
    assert cw.folder == "test"
    assert cw.is_admin is True
    assert cw.tenant_id == "t1"
