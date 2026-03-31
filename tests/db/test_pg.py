"""Tests for rolemesh.db (PostgreSQL)."""

from __future__ import annotations

import pytest

from rolemesh.core.types import NewMessage, RegisteredGroup, ScheduledTask
from rolemesh.db.pg import (
    create_task,
    delete_task,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_due_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    get_session,
    get_task_by_id,
    get_tasks_for_group,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
    update_task,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def test_store_and_get_messages() -> None:
    await store_chat_metadata("chat1", "2024-01-01T00:00:00Z", name="Test Chat")
    msg = NewMessage(
        id="m1",
        chat_jid="chat1",
        sender="user1",
        sender_name="Alice",
        content="Hello",
        timestamp="2024-01-01T00:00:01Z",
    )
    await store_message(msg)

    messages, new_ts = await get_new_messages(["chat1"], "2024-01-01T00:00:00Z", "Andy")
    assert len(messages) == 1
    assert messages[0].content == "Hello"
    assert new_ts > "2024-01-01T00:00:00Z"


async def test_get_messages_since() -> None:
    await store_chat_metadata("chat1", "2024-01-01T00:00:00Z")
    await store_message(
        NewMessage(
            id="m1",
            chat_jid="chat1",
            sender="user1",
            sender_name="Alice",
            content="Hello",
            timestamp="2024-01-01T00:00:01Z",
        )
    )
    await store_message(
        NewMessage(
            id="m2",
            chat_jid="chat1",
            sender="user1",
            sender_name="Alice",
            content="World",
            timestamp="2024-01-01T00:00:02Z",
        )
    )
    msgs = await get_messages_since("chat1", "2024-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 2


async def test_router_state() -> None:
    await set_router_state("test_key", "test_value")
    assert await get_router_state("test_key") == "test_value"
    assert await get_router_state("missing") is None


async def test_sessions() -> None:
    await set_session("group1", "sess-123")
    assert await get_session("group1") == "sess-123"
    assert await get_session("missing") is None

    sessions = await get_all_sessions()
    assert sessions["group1"] == "sess-123"


async def test_registered_groups() -> None:
    group = RegisteredGroup(
        name="Test Group",
        folder="testgroup",
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
    )
    await set_registered_group("chat@jid", group)

    groups = await get_all_registered_groups()
    assert "chat@jid" in groups
    assert groups["chat@jid"].name == "Test Group"
    assert groups["chat@jid"].folder == "testgroup"


async def test_task_crud() -> None:
    task = ScheduledTask(
        id="t1",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Do something",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
        next_run="2024-01-02T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    await create_task(task)

    retrieved = await get_task_by_id("t1")
    assert retrieved is not None
    assert retrieved.prompt == "Do something"

    tasks = await get_tasks_for_group("testgroup")
    assert len(tasks) == 1

    all_tasks = await get_all_tasks()
    assert len(all_tasks) == 1

    await update_task("t1", prompt="Updated prompt")
    updated = await get_task_by_id("t1")
    assert updated is not None
    assert updated.prompt == "Updated prompt"

    await delete_task("t1")
    assert await get_task_by_id("t1") is None


async def test_get_due_tasks() -> None:
    task = ScheduledTask(
        id="t2",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Past task",
        schedule_type="once",
        schedule_value="2020-01-01T00:00:00Z",
        context_mode="isolated",
        next_run="2020-01-01T00:00:00Z",
        status="active",
        created_at="2020-01-01T00:00:00Z",
    )
    await create_task(task)

    due = await get_due_tasks()
    assert len(due) >= 1
    assert any(t.id == "t2" for t in due)


async def test_store_chat_metadata_with_channel() -> None:
    await store_chat_metadata("tg:123", "2024-01-01T00:00:00Z", name="TG Chat", channel="telegram", is_group=True)
    from rolemesh.db.pg import get_all_chats

    chats = await get_all_chats()
    assert any(c.jid == "tg:123" for c in chats)


async def test_store_chat_metadata_no_name() -> None:
    await store_chat_metadata("chat2", "2024-01-01T00:00:00Z")
    from rolemesh.db.pg import get_all_chats

    chats = await get_all_chats()
    assert any(c.jid == "chat2" for c in chats)


async def test_update_chat_name() -> None:
    from rolemesh.db.pg import update_chat_name

    await store_chat_metadata("chat3", "2024-01-01T00:00:00Z", name="Old Name")
    await update_chat_name("chat3", "New Name")
    from rolemesh.db.pg import get_all_chats

    chats = await get_all_chats()
    chat = next(c for c in chats if c.jid == "chat3")
    assert chat.name == "New Name"


async def test_group_sync() -> None:
    from rolemesh.db.pg import get_last_group_sync, set_last_group_sync

    assert await get_last_group_sync() is None
    await set_last_group_sync()
    assert await get_last_group_sync() is not None


async def test_update_task_after_run() -> None:
    from rolemesh.db.pg import log_task_run, update_task_after_run

    task = ScheduledTask(
        id="t3",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Test",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
        next_run="2024-01-02T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    await create_task(task)
    await update_task_after_run("t3", "2024-01-03T09:00:00Z", "Done")
    updated = await get_task_by_id("t3")
    assert updated is not None
    assert updated.last_result == "Done"
    assert updated.next_run == "2024-01-03T09:00:00Z"

    from rolemesh.core.types import TaskRunLog

    await log_task_run(TaskRunLog(task_id="t3", run_at="2024-01-02T09:00:00Z", duration_ms=500, status="success"))


async def test_update_task_multiple_fields() -> None:
    task = ScheduledTask(
        id="t4",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Original",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="group",
        next_run="2024-01-02T09:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    await create_task(task)
    await update_task("t4", prompt="New prompt", status="paused", schedule_value="0 10 * * *")
    updated = await get_task_by_id("t4")
    assert updated is not None
    assert updated.prompt == "New prompt"
    assert updated.status == "paused"


async def test_update_task_no_fields() -> None:
    task = ScheduledTask(
        id="t5",
        group_folder="testgroup",
        chat_jid="chat@jid",
        prompt="Test",
        schedule_type="once",
        schedule_value="2024-01-01T00:00:00Z",
        context_mode="isolated",
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )
    await create_task(task)
    await update_task("t5")  # No fields — should be a no-op


async def test_store_message_direct() -> None:
    from rolemesh.db.pg import store_message_direct

    await store_chat_metadata("chat5", "2024-01-01T00:00:00Z")
    await store_message_direct(
        id="md1",
        chat_jid="chat5",
        sender="user1",
        sender_name="Alice",
        content="Direct message",
        timestamp="2024-01-01T00:00:01Z",
        is_from_me=False,
    )
    msgs = await get_messages_since("chat5", "2024-01-01T00:00:00Z", "Andy")
    assert len(msgs) == 1


async def test_registered_group_with_config() -> None:
    from rolemesh.core.types import ContainerConfig

    group = RegisteredGroup(
        name="Config Group",
        folder="configgroup",
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
        container_config=ContainerConfig(timeout=600000),
        is_main=True,
    )
    await set_registered_group("config@jid", group)
    groups = await get_all_registered_groups()
    assert "config@jid" in groups
    assert groups["config@jid"].is_main is True
