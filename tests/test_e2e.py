"""End-to-end tests for the RoleMesh Python runtime engine.

Strategy: Test core logic units with real PostgreSQL (testcontainers).
The main.py orchestrator internals have been refactored to use OrchestratorState,
so we test the key building blocks individually.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def e2e_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> Path:
    """Set up isolated E2E environment with tmp dirs and PG test DB."""
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    store_dir = tmp_path / "store"
    data_dir.mkdir()
    groups_dir.mkdir()
    store_dir.mkdir()

    monkeypatch.setattr("rolemesh.core.config.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.config.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("rolemesh.core.config.STORE_DIR", store_dir)
    monkeypatch.setattr("rolemesh.core.config.POLL_INTERVAL", 0.05)
    monkeypatch.setattr("rolemesh.core.config.SCHEDULER_POLL_INTERVAL", 0.05)
    monkeypatch.setattr("rolemesh.core.config.IDLE_TIMEOUT", 1000)
    monkeypatch.setattr("rolemesh.core.config.TIMEZONE", "UTC")
    monkeypatch.setattr("rolemesh.core.config.ASSISTANT_NAME", "Andy")

    monkeypatch.setattr("rolemesh.core.group_folder.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.group_folder.GROUPS_DIR", groups_dir)

    from rolemesh.db.pg import _init_test_database

    await _init_test_database(pg_url)

    yield tmp_path

    from rolemesh.db.pg import close_database

    await close_database()


# ===========================================================================
# Tests
# ===========================================================================


async def test_multi_tenant_schema_creation(e2e_env: Path) -> None:
    """All new tables are created correctly."""
    from rolemesh.db.pg import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
    )

    tenant = await create_tenant(name="Test Corp", slug="test-corp")
    assert tenant.id
    assert tenant.name == "Test Corp"
    assert tenant.max_concurrent_containers == 5

    coworker = await create_coworker(
        tenant_id=tenant.id,
        name="Test Bot",
        folder="test-bot",
        agent_role="super_agent",
    )
    assert coworker.id
    assert coworker.agent_role == "super_agent"

    binding = await create_channel_binding(
        coworker_id=coworker.id,
        tenant_id=tenant.id,
        channel_type="telegram",
        credentials={"bot_token": "test-token"},
    )
    assert binding.id
    assert binding.channel_type == "telegram"

    conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=coworker.id,
        channel_binding_id=binding.id,
        channel_chat_id="12345",
        name="Test Chat",
        requires_trigger=False,
    )
    assert conv.id
    assert conv.requires_trigger is False


async def test_session_per_conversation(e2e_env: Path) -> None:
    """Sessions are keyed by conversation_id."""
    from rolemesh.db.pg import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
        get_session,
        set_session,
    )

    tenant = await create_tenant(name="T", slug="t-sess")
    cw = await create_coworker(tenant_id=tenant.id, name="Bot", folder="bot")
    b = await create_channel_binding(coworker_id=cw.id, tenant_id=tenant.id, channel_type="tg")
    conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id="999",
    )

    assert await get_session(conv.id) is None
    await set_session(conv.id, tenant.id, cw.id, "sess-123")
    assert await get_session(conv.id) == "sess-123"


async def test_messages_per_conversation(e2e_env: Path) -> None:
    """Messages are stored and retrieved per conversation."""
    from rolemesh.db.pg import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
        get_messages_since,
        store_message,
    )

    tenant = await create_tenant(name="T", slug="t-msg")
    cw = await create_coworker(tenant_id=tenant.id, name="Bot", folder="bot2")
    b = await create_channel_binding(coworker_id=cw.id, tenant_id=tenant.id, channel_type="tg")
    conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id="888",
    )

    await store_message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        msg_id="m1",
        sender="user1",
        sender_name="User",
        content="Hello",
        timestamp="2024-06-01T12:00:00+00:00",
    )

    msgs = await get_messages_since(tenant.id, conv.id, "", "Bot")
    assert len(msgs) == 1
    assert msgs[0].content == "Hello"


async def test_tasks_per_coworker(e2e_env: Path) -> None:
    """Tasks are created per coworker and retrieved correctly."""
    from rolemesh.core.types import ScheduledTask
    from rolemesh.db.pg import (
        create_coworker,
        create_task,
        create_tenant,
        get_task_by_id,
        get_tasks_for_coworker,
    )

    tenant = await create_tenant(name="T", slug="t-task")
    cw = await create_coworker(tenant_id=tenant.id, name="Bot", folder="bot3")

    import uuid

    task_id = str(uuid.uuid4())
    await create_task(
        ScheduledTask(
            id=task_id,
            tenant_id=tenant.id,
            coworker_id=cw.id,
            prompt="Test task",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2024-06-01T09:00:00+00:00",
            status="active",
        )
    )

    task = await get_task_by_id(task_id)
    assert task is not None
    assert task.prompt == "Test task"

    tasks = await get_tasks_for_coworker(cw.id)
    assert len(tasks) == 1


async def test_orchestrator_state_three_level_concurrency(e2e_env: Path) -> None:
    """OrchestratorState enforces global + tenant + coworker limits."""
    from rolemesh.core.orchestrator_state import OrchestratorState
    from rolemesh.core.types import Tenant

    state = OrchestratorState(global_limit=3)
    state.tenants["t1"] = Tenant(id="t1", name="T1", max_concurrent_containers=2)

    assert state.can_start_container("t1", "cw1") is True

    state.increment_active("t1", "cw1")
    state.increment_active("t1", "cw1")
    # cw1 has max_concurrent=2 by default (no coworker state loaded), so global check
    assert state.global_active == 2

    state.increment_active("t1", "cw2")
    # tenant t1 has max=2, now at 3 → should fail tenant limit
    assert state.can_start_container("t1", "cw3") is False

    # But a different tenant should work (if global < 3)
    state.tenants["t2"] = Tenant(id="t2", name="T2", max_concurrent_containers=5)
    assert state.can_start_container("t2", "cw4") is False  # global is at 3 == limit

    state.decrement_active("t1", "cw1")
    assert state.can_start_container("t2", "cw4") is True


async def test_queue_concurrency(e2e_env: Path) -> None:
    """Multiple groups enqueue → respects concurrency limit."""
    from rolemesh.container.scheduler import GroupQueue

    queue = GroupQueue()
    processing_order: list[str] = []
    active_count: list[int] = []

    async def process_fn(group_jid: str) -> bool:
        active_count.append(sum(1 for s in queue._groups.values() if s.active))
        processing_order.append(group_jid)
        await asyncio.sleep(0.05)
        return True

    queue.set_process_messages_fn(process_fn)

    queue.enqueue_message_check("group1")
    queue.enqueue_message_check("group2")
    queue.enqueue_message_check("group3")

    await asyncio.sleep(0.5)

    assert "group1" in processing_order
    assert "group2" in processing_order
    assert "group3" in processing_order

    for count in active_count:
        assert count <= 5


async def test_scheduled_task_compute_next_run(e2e_env: Path) -> None:
    """compute_next_run correctly calculates next execution time."""
    from rolemesh.core.types import ScheduledTask
    from rolemesh.orchestration.task_scheduler import compute_next_run

    task_once = ScheduledTask(
        id="t1",
        tenant_id="t",
        coworker_id="cw",
        prompt="once",
        schedule_type="once",
        schedule_value="2024-01-01T00:00:00",
        context_mode="isolated",
        next_run="2024-01-01T00:00:00Z",
        status="active",
    )
    assert compute_next_run(task_once) is None

    task_cron = ScheduledTask(
        id="t2",
        tenant_id="t",
        coworker_id="cw",
        prompt="cron",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="isolated",
        next_run="2024-01-01T09:00:00Z",
        status="active",
    )
    next_cron = compute_next_run(task_cron)
    assert next_cron is not None

    task_interval = ScheduledTask(
        id="t3",
        tenant_id="t",
        coworker_id="cw",
        prompt="interval",
        schedule_type="interval",
        schedule_value="3600000",
        context_mode="isolated",
        next_run="2020-01-01T00:00:00Z",
        status="active",
    )
    next_interval = compute_next_run(task_interval)
    assert next_interval is not None
    assert next_interval > "2024-01-01T00:00:00Z"
