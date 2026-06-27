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


async def test_request_shutdown_no_active() -> None:
    queue = GroupQueue()
    queue.request_shutdown("group1")  # Should not raise


async def test_interrupt_current_turn_no_active() -> None:
    queue = GroupQueue()
    queue.interrupt_current_turn("group1")  # Should not raise


async def test_interrupt_current_turn_active_no_transport() -> None:
    queue = GroupQueue()
    state = queue._get_group("group1")
    state.active = True
    state.job_id = "job-abc"
    # No transport configured — should gracefully no-op, not raise
    queue.interrupt_current_turn("group1")
    # State is unchanged (interrupt doesn't touch state)
    assert state.active is True
    assert state.job_id == "job-abc"


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


async def test_slot_released_at_is_final_container_stays_warm() -> None:
    """Slot-follows-turn: notify_idle (is_final) frees the turn slot while the
    container stays live (warm). The container slot is freed only at exit."""
    state = OrchestratorState(global_limit=5)
    queue = GroupQueue(orchestrator_state=state)

    went_warm = asyncio.Event()
    may_exit = asyncio.Event()

    async def process(group_jid: str) -> bool:
        # Turn finished (is_final) → go warm, but keep the container alive.
        queue.notify_idle(group_jid)
        went_warm.set()
        await may_exit.wait()
        return True

    queue.set_process_messages_fn(process)
    queue.enqueue_message_check("g1", tenant_id="t1", coworker_id="cw1")

    await asyncio.wait_for(went_warm.wait(), timeout=1.0)
    gs = queue._get_group("g1")
    # Turn slot released, but the container is still alive and warm.
    assert state.global_active == 0
    assert state.coworker_active.get("cw1", 0) == 0
    assert state.live_containers == 1
    assert gs.active is True
    assert gs.processing is False
    assert gs.idle_waiting is True
    assert "g1" in queue._warm

    # Container exits → live counter returns to zero, warm entry cleared.
    may_exit.set()
    await asyncio.sleep(0.05)
    assert state.live_containers == 0
    assert "g1" not in queue._warm
    assert gs.active is False


class _FakeRuntime:
    """Minimal runtime exposing list_live for reaper tests."""

    def __init__(self, live: set[str]) -> None:
        self._live = live

    async def list_live(self, prefix: str) -> set[str]:
        return {n for n in self._live if n.startswith(prefix)}


async def test_reaper_reaps_ghost_after_two_sweeps() -> None:
    """A group marked active whose container is no longer live is reaped after
    two confirming sweeps, releasing its leaked turn + container counters."""
    state = OrchestratorState(global_limit=5)
    runtime = _FakeRuntime(live=set())  # nothing alive
    queue = GroupQueue(runtime=runtime, orchestrator_state=state)  # type: ignore[arg-type]

    # Simulate a leaked slot: active group, registered container, counters held,
    # but the container is gone (not in list_live).
    gs = queue._get_group("ghost")
    gs.tenant_id, gs.coworker_id = "t1", "cw1"
    queue._acquire_container(gs)
    gs.processing = True
    queue._acquire_turn(gs)
    queue.register_process("ghost", "rolemesh-ghost-123")
    assert state.global_active == 1
    assert state.live_containers == 1

    # First sweep: suspected, not yet reaped (two-sweep confirmation).
    assert await queue.reconcile_once() == 0
    assert gs.active is True
    assert gs.missing_sweeps == 1

    # Second sweep: confirmed → reaped, counters reconciled to zero.
    assert await queue.reconcile_once() == 1
    assert gs.active is False
    assert state.global_active == 0
    assert state.live_containers == 0


async def test_reaper_spares_live_and_unregistered() -> None:
    """A group whose container is live, and one not yet registered, are spared."""
    state = OrchestratorState(global_limit=5)
    runtime = _FakeRuntime(live={"rolemesh-alive-1"})
    queue = GroupQueue(runtime=runtime, orchestrator_state=state)  # type: ignore[arg-type]

    alive = queue._get_group("alive")
    queue._acquire_container(alive)
    queue.register_process("alive", "rolemesh-alive-1")

    starting = queue._get_group("starting")
    queue._acquire_container(starting)  # active but no container_name yet

    assert await queue.reconcile_once() == 0
    assert await queue.reconcile_once() == 0
    assert alive.active is True
    assert starting.active is True


async def test_warm_container_does_not_wedge_other_coworker() -> None:
    """A warm idle container holds no turn slot, so it cannot wedge a second
    coworker's cold start at the coworker turn ceiling."""
    state = OrchestratorState(global_limit=5)
    queue = GroupQueue(orchestrator_state=state)

    warm = asyncio.Event()
    g2_started = asyncio.Event()
    release = asyncio.Event()

    async def process(group_jid: str) -> bool:
        if group_jid == "g1":
            queue.notify_idle(group_jid)  # g1 goes warm immediately
            warm.set()
            await release.wait()
        else:
            g2_started.set()
            await release.wait()
        return True

    queue.set_process_messages_fn(process)
    queue.enqueue_message_check("g1", tenant_id="t1", coworker_id="cw1")
    await asyncio.wait_for(warm.wait(), timeout=1.0)

    # g2 (different coworker) cold-starts even though g1's container is alive.
    queue.enqueue_message_check("g2", tenant_id="t1", coworker_id="cw2")
    await asyncio.wait_for(g2_started.wait(), timeout=1.0)
    assert state.live_containers == 2  # both containers alive
    assert state.global_active == 1  # only g2 holds a turn slot
    release.set()
    await asyncio.sleep(0.05)
