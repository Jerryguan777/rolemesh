"""Tests for rolemesh.types."""

from rolemesh.core.types import (
    AdditionalMount,
    ContainerConfig,
    NewMessage,
    RegisteredGroup,
    ScheduledTask,
    TaskRunLog,
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
        group_folder="test",
        chat_jid="chat@jid",
        prompt="Do something",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
    )
    assert task.status == "active"
    assert task.next_run is None


def test_task_run_log() -> None:
    log = TaskRunLog(task_id="t1", run_at="2024-01-01T00:00:00Z", duration_ms=1000, status="success")
    assert log.result is None
    assert log.error is None
