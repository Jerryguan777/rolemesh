"""Tests for rolemesh.db (PostgreSQL) — multi-tenant schema."""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.types import McpServerConfig, ScheduledTask, TaskRunLog
from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_task,
    create_tenant,
    create_user,
    delete_task,
    get_all_sessions,
    get_all_tasks,
    get_all_tenants,
    get_channel_binding,
    get_conversation,
    get_conversation_by_binding_and_chat,
    get_coworker,
    get_coworker_by_folder,
    get_due_tasks,
    get_messages_since,
    get_session,
    get_task_by_id,
    get_tasks_for_coworker,
    get_tenant,
    get_tenant_by_slug,
    log_task_run,
    set_session,
    store_message,
    update_conversation_last_invocation,
    update_task,
    update_task_after_run,
    update_tenant_message_cursor,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Helper: create full entity chain
# ---------------------------------------------------------------------------


async def _create_chain() -> tuple[str, str, str, str]:
    """Create tenant → coworker → binding → conversation. Return IDs."""
    t = await create_tenant(name="Test Corp", slug=f"test-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(tenant_id=t.id, name="Bot", folder=f"bot-{uuid.uuid4().hex[:8]}")
    b = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id, channel_type="telegram", credentials={"bot_token": "x"}
    )
    conv = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id, channel_binding_id=b.id, channel_chat_id="12345"
    )
    return t.id, cw.id, b.id, conv.id


# ---------------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------------


async def test_create_and_get_tenant() -> None:
    t = await create_tenant(name="Acme", slug="acme", max_concurrent_containers=10)
    assert t.id
    assert t.name == "Acme"
    assert t.max_concurrent_containers == 10

    fetched = await get_tenant(t.id)
    assert fetched is not None
    assert fetched.name == "Acme"

    by_slug = await get_tenant_by_slug("acme")
    assert by_slug is not None
    assert by_slug.id == t.id


async def test_get_all_tenants() -> None:
    await create_tenant(name="T1", slug=f"t1-{uuid.uuid4().hex[:8]}")
    await create_tenant(name="T2", slug=f"t2-{uuid.uuid4().hex[:8]}")
    tenants = await get_all_tenants()
    assert len(tenants) >= 2


async def test_update_tenant_message_cursor() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    await update_tenant_message_cursor(t.id, "2024-06-01T12:00:00+00:00")
    fetched = await get_tenant(t.id)
    assert fetched is not None
    assert fetched.last_message_cursor is not None


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


async def test_create_user() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(tenant_id=t.id, name="Alice", email="alice@example.com", role="admin")
    assert u.id
    assert u.name == "Alice"
    assert u.role == "admin"


# ---------------------------------------------------------------------------
# Role CRUD
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Coworker CRUD
# ---------------------------------------------------------------------------


async def test_create_and_get_coworker() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id,
        name="Ops Bot",
        folder="ops-bot",
        agent_role="super_agent",
        max_concurrent=3,
        tools=[
            McpServerConfig(name="my-mcp-server", type="sse", url="http://localhost:9100/mcp/"),
        ],
    )
    assert cw.id
    assert cw.agent_role == "super_agent"
    assert cw.max_concurrent == 3
    assert cw.tools == [McpServerConfig(name="my-mcp-server", type="sse", url="http://localhost:9100/mcp/")]
    assert cw.agent_backend == "claude"

    fetched = await get_coworker(cw.id, tenant_id=t.id)
    assert fetched is not None
    assert fetched.name == "Ops Bot"

    by_folder = await get_coworker_by_folder(t.id, "ops-bot")
    assert by_folder is not None
    assert by_folder.id == cw.id


# ---------------------------------------------------------------------------
# ChannelBinding CRUD
# ---------------------------------------------------------------------------


async def test_channel_binding() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(tenant_id=t.id, name="Bot", folder=f"bot-{uuid.uuid4().hex[:8]}")
    b = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id, channel_type="telegram", credentials={"bot_token": "abc"}
    )
    assert b.id
    assert b.credentials["bot_token"] == "abc"

    fetched = await get_channel_binding(b.id, tenant_id=t.id)
    assert fetched is not None
    assert fetched.channel_type == "telegram"


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------


async def test_conversation_crud() -> None:
    _tid, _cwid, bid, convid = await _create_chain()
    conv = await get_conversation(convid, tenant_id=_tid)
    assert conv is not None
    assert conv.channel_chat_id == "12345"
    assert conv.requires_trigger is True

    by_bc = await get_conversation_by_binding_and_chat(bid, "12345", tenant_id=_tid)
    assert by_bc is not None
    assert by_bc.id == convid

    await update_conversation_last_invocation(convid, "2024-06-01T12:00:00+00:00", tenant_id=_tid)
    updated = await get_conversation(convid, tenant_id=_tid)
    assert updated is not None
    assert updated.last_agent_invocation is not None


# ---------------------------------------------------------------------------
# Sessions (per-conversation)
# ---------------------------------------------------------------------------


async def test_sessions_per_conversation() -> None:
    tid, cwid, _, convid = await _create_chain()
    assert await get_session(convid, tenant_id=tid) is None
    await set_session(convid, tid, cwid, "sess-abc")
    assert await get_session(convid, tenant_id=tid) == "sess-abc"

    sessions = await get_all_sessions()
    assert convid in sessions


# ---------------------------------------------------------------------------
# Messages (per-conversation)
# ---------------------------------------------------------------------------


async def test_store_and_get_messages() -> None:
    tid, _, _, convid = await _create_chain()
    await store_message(
        tenant_id=tid,
        conversation_id=convid,
        msg_id="m1",
        sender="user1",
        sender_name="Alice",
        content="Hello",
        timestamp="2024-01-01T00:00:01+00:00",
    )
    msgs = await get_messages_since(tid, convid, "", "Bot")
    assert len(msgs) == 1
    assert msgs[0].content == "Hello"


async def test_get_messages_since_filter() -> None:
    tid, _, _, convid = await _create_chain()
    await store_message(
        tenant_id=tid,
        conversation_id=convid,
        msg_id="m1",
        sender="u",
        sender_name="U",
        content="First",
        timestamp="2024-01-01T00:00:01+00:00",
    )
    await store_message(
        tenant_id=tid,
        conversation_id=convid,
        msg_id="m2",
        sender="u",
        sender_name="U",
        content="Second",
        timestamp="2024-01-01T00:00:02+00:00",
    )
    msgs = await get_messages_since(tid, convid, "2024-01-01T00:00:01+00:00", "Bot")
    assert len(msgs) == 1
    assert msgs[0].content == "Second"


# ---------------------------------------------------------------------------
# Scheduled Tasks (per-coworker)
# ---------------------------------------------------------------------------


async def test_task_crud() -> None:
    tid, cwid, _, _ = await _create_chain()
    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=tid,
            coworker_id=cwid,
            prompt="Do something",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="group",
            next_run="2024-01-02T09:00:00+00:00",
            status="active",
        )
    )

    retrieved = await get_task_by_id(task_id, tenant_id=tid)
    assert retrieved is not None
    assert retrieved.prompt == "Do something"

    tasks = await get_tasks_for_coworker(cwid, tenant_id=tid)
    assert len(tasks) == 1

    all_tasks = await get_all_tasks(tid)
    assert len(all_tasks) == 1

    await update_task(task_id, tenant_id=tid, prompt="Updated prompt")
    updated = await get_task_by_id(task_id, tenant_id=tid)
    assert updated is not None
    assert updated.prompt == "Updated prompt"

    await delete_task(task_id, tenant_id=tid)
    assert await get_task_by_id(task_id, tenant_id=tid) is None


async def test_get_due_tasks() -> None:
    tid, cwid, _, _ = await _create_chain()
    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=tid,
            coworker_id=cwid,
            prompt="Past task",
            schedule_type="once",
            schedule_value="2020-01-01T00:00:00+00:00",
            context_mode="isolated",
            next_run="2020-01-01T00:00:00+00:00",
            status="active",
        )
    )
    due = await get_due_tasks()
    assert any(t.id == task_id for t in due)


async def test_update_task_after_run() -> None:
    tid, cwid, _, _ = await _create_chain()
    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=tid,
            coworker_id=cwid,
            prompt="Test",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="group",
            next_run="2024-01-02T09:00:00+00:00",
            status="active",
        )
    )
    await update_task_after_run(task_id, "2024-01-03T09:00:00+00:00", "Done", tenant_id=tid)
    updated = await get_task_by_id(task_id, tenant_id=tid)
    assert updated is not None
    assert "Done" in (updated.last_result or "")


async def test_log_task_run() -> None:
    tid, cwid, _, _ = await _create_chain()
    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=tid,
            coworker_id=cwid,
            prompt="Test",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="group",
            next_run="2024-01-02T09:00:00+00:00",
            status="active",
        )
    )
    await log_task_run(
        TaskRunLog(
            tenant_id=tid,
            task_id=task_id,
            run_at="2024-01-02T09:00:00+00:00",
            duration_ms=500,
            status="success",
        )
    )
