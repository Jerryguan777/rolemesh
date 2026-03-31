"""Tests for rolemesh.group_queue."""

from __future__ import annotations

import asyncio

from rolemesh.container.scheduler import GroupQueue
from rolemesh.core.orchestrator_state import OrchestratorState


async def test_basic_message_enqueue() -> None:
    queue = GroupQueue()
    called = False

    async def process_messages(group_jid: str) -> bool:
        nonlocal called
        called = True
        return True

    queue.set_process_messages_fn(process_messages)
    queue.enqueue_message_check("group1")
    await asyncio.sleep(0.1)
    assert called


async def test_task_enqueue() -> None:
    queue = GroupQueue()
    executed = False

    async def task_fn() -> None:
        nonlocal executed
        executed = True

    queue.enqueue_task("group1", "task1", task_fn)
    await asyncio.sleep(0.1)
    assert executed


async def test_send_message_no_active() -> None:
    queue = GroupQueue()
    assert queue.send_message("group1", "hello") is False


async def test_shutdown() -> None:
    queue = GroupQueue()
    await queue.shutdown()


async def test_duplicate_task_skipped() -> None:
    queue = GroupQueue()
    count = 0

    async def task_fn() -> None:
        nonlocal count
        count += 1
        await asyncio.sleep(0.5)

    queue.enqueue_task("group1", "task1", task_fn)
    await asyncio.sleep(0.05)
    queue.enqueue_task("group1", "task1", task_fn)
    await asyncio.sleep(0.6)
    assert count == 1


async def test_notify_idle() -> None:
    queue = GroupQueue()
    state = queue._get_group("group1")
    state.active = True
    queue.notify_idle("group1")
    assert state.idle_waiting is True


async def test_close_stdin_no_active() -> None:
    queue = GroupQueue()
    queue.close_stdin("group1")  # Should not raise


async def test_enqueue_message_while_active() -> None:
    queue = GroupQueue()
    call_count = 0

    async def process_messages(group_jid: str) -> bool:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.3)
        return True

    queue.set_process_messages_fn(process_messages)
    queue.enqueue_message_check("group1")
    await asyncio.sleep(0.05)  # Let first run start
    queue.enqueue_message_check("group1")  # Should queue, not start new
    await asyncio.sleep(0.5)  # Let drain happen
    assert call_count >= 1


async def test_enqueue_task_while_active() -> None:
    queue = GroupQueue()

    async def slow_process(group_jid: str) -> bool:
        await asyncio.sleep(0.5)
        return True

    queue.set_process_messages_fn(slow_process)
    queue.enqueue_message_check("group1")
    await asyncio.sleep(0.05)

    executed = False

    async def task_fn() -> None:
        nonlocal executed
        executed = True

    queue.enqueue_task("group1", "t1", task_fn)
    await asyncio.sleep(0.8)
    assert executed


async def test_concurrency_limit_via_orchestrator_state() -> None:
    """Three-level concurrency: OrchestratorState with global_limit=1."""
    state = OrchestratorState(global_limit=1)
    queue = GroupQueue(orchestrator_state=state)

    async def slow_process(group_jid: str) -> bool:
        await asyncio.sleep(0.3)
        return True

    queue.set_process_messages_fn(slow_process)

    queue.enqueue_message_check("group1", tenant_id="t1", coworker_id="cw1")
    await asyncio.sleep(0.05)
    queue.enqueue_message_check("group2", tenant_id="t1", coworker_id="cw2")  # Should be queued
    state2 = queue._get_group("group2")
    assert state2.pending_messages is True
    await asyncio.sleep(0.5)
