"""GroupQueue idle suspend/resume for HITL approval (docs/12-hitl-approval-architecture.md §8).

Timer-lifecycle focused: these prove the suspend closes all three reaping paths,
that resume re-arms idle exactly once (and only when the last pending approval
drains), and that an adopted (restart-recovered) container is reaped *and*
torn down. Real ``GroupQueue`` + a fake NATS transport that records every
``agent.{job_id}.shutdown`` request — the observable reaping signal.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from rolemesh.container.scheduler import GroupQueue, _QueuedTask


class _FakeNats:
    def __init__(self) -> None:
        self.shutdown_subjects: list[str] = []

    async def request(self, subject: str, _payload: bytes, timeout: float | None = None) -> Any:
        self.shutdown_subjects.append(subject)
        return SimpleNamespace(data=b"ack")


class _FakeJS:
    async def publish(self, _subject: str, _payload: bytes) -> None:
        return None


class _FakeTransport:
    def __init__(self) -> None:
        self.nc = _FakeNats()
        self.js = _FakeJS()


def _active_group(q: GroupQueue, key: str = "conv1", job_id: str = "job1") -> Any:
    state = q._get_group(key)
    state.active = True
    state.job_id = job_id
    return state


async def test_suspend_cancels_idle_timer_and_blocks_rearm() -> None:
    q = GroupQueue(transport=_FakeTransport(), idle_timeout_ms=10_000)
    state = _active_group(q)
    q.arm_idle_timer("conv1")
    assert state.idle_handle is not None

    q.suspend_for_approval("conv1", "req1")
    assert state.idle_handle is None
    assert state.idle_waiting is False
    assert "req1" in state.awaiting_approval

    # A status/tool event during the wait tries to re-arm — must be refused so
    # the held container is not reaped mid-approval.
    q.arm_idle_timer("conv1")
    assert state.idle_handle is None


async def test_resume_when_set_empty_rearms_and_reaps() -> None:
    ft = _FakeTransport()
    q = GroupQueue(transport=ft, idle_timeout_ms=30)
    state = _active_group(q)

    q.suspend_for_approval("conv1", "req1")
    assert q.resume_from_approval("conv1", "req1") is True
    assert state.idle_handle is not None

    await asyncio.sleep(0.08)
    assert ft.nc.shutdown_subjects == ["agent.job1.shutdown"]


async def test_suspended_container_not_reaped_during_wait() -> None:
    # suspend → (container's own approval timeout window) → still not reaped by
    # the orchestrator idle path. Teardown is the container's job, not idle's.
    ft = _FakeTransport()
    q = GroupQueue(transport=ft, idle_timeout_ms=20)
    state = _active_group(q)

    q.suspend_for_approval("conv1", "req1")
    await asyncio.sleep(0.06)  # well past idle_timeout_ms

    assert ft.nc.shutdown_subjects == []
    assert "req1" in state.awaiting_approval


async def test_concurrent_double_approval_only_last_rearms() -> None:
    ft = _FakeTransport()
    q = GroupQueue(transport=ft, idle_timeout_ms=30)
    state = _active_group(q)

    q.suspend_for_approval("conv1", "req1")
    q.suspend_for_approval("conv1", "req2")

    # First decision: req2 still pending ⇒ must NOT re-arm.
    assert q.resume_from_approval("conv1", "req1") is True
    assert state.idle_handle is None
    await asyncio.sleep(0.06)
    assert ft.nc.shutdown_subjects == []

    # Last decision: set drains ⇒ re-arm and reap.
    assert q.resume_from_approval("conv1", "req2") is True
    assert state.idle_handle is not None
    await asyncio.sleep(0.06)
    assert ft.nc.shutdown_subjects == ["agent.job1.shutdown"]


async def test_decision_then_cancel_no_double_rearm() -> None:
    # S2 emits approval_cancel on reject too, so the orchestrator can see a
    # decision and then a cancel for the same request. The cancel must not
    # re-arm idle a second time or clear a sibling's suspend.
    q = GroupQueue(transport=_FakeTransport(), idle_timeout_ms=10_000)
    state = _active_group(q)

    q.suspend_for_approval("conv1", "req1")
    assert q.resume_from_approval("conv1", "req1") is True
    handle_after_decision = state.idle_handle
    assert handle_after_decision is not None

    assert q.resume_from_approval("conv1", "req1") is False
    assert state.idle_handle is handle_after_decision


async def test_resume_of_unknown_request_is_noop() -> None:
    q = GroupQueue(transport=_FakeTransport(), idle_timeout_ms=10_000)
    q._get_group("conv1")
    assert q.resume_from_approval("conv1", "never-suspended") is False


async def test_enqueue_task_while_suspended_does_not_reap() -> None:
    # Reaping path C: a task enqueued onto an active+idle_waiting container
    # preempts it. The suspend forces idle_waiting False, so enqueue must queue
    # the task without requesting a shutdown.
    ft = _FakeTransport()
    q = GroupQueue(transport=ft, idle_timeout_ms=10_000)
    state = _active_group(q)
    q.suspend_for_approval("conv1", "req1")

    async def _task() -> None:
        return None

    q.enqueue_task("conv1", "t1", _task)
    await asyncio.sleep(0.02)

    assert ft.nc.shutdown_subjects == []
    assert any(t.id == "t1" for t in state.pending_tasks)


async def test_notify_idle_while_suspended_does_not_reap() -> None:
    # Reaping path B: notify_idle with a queued task preempts. The suspend must
    # win — neither flip idle_waiting nor request a shutdown.
    ft = _FakeTransport()
    q = GroupQueue(transport=ft, idle_timeout_ms=10_000)
    state = _active_group(q)

    async def _task() -> None:
        return None

    state.pending_tasks.append(_QueuedTask(id="t1", group_jid="conv1", fn=_task))
    q.suspend_for_approval("conv1", "req1")
    q.notify_idle("conv1")

    assert state.idle_waiting is False
    assert ft.nc.shutdown_subjects == []


async def test_adopt_orphan_reaped_clears_state() -> None:
    # Restart recovery: an adopted container has no _run_for_group finally, so
    # after the idle reaper asks it to wind down it must reset the rebuilt state
    # inline — otherwise the conversation is wedged active forever.
    ft = _FakeTransport()
    q = GroupQueue(transport=ft, idle_timeout_ms=30)
    q.adopt_orphan_container("conv1", job_id="job1", tenant_id="t1", coworker_id="cw1")
    state = q._get_group("conv1")
    assert state.active is True
    assert state.adopted is True

    q.suspend_for_approval("conv1", "req1")
    assert q.resume_from_approval("conv1", "req1") is True
    assert state.idle_handle is not None

    await asyncio.sleep(0.08)
    assert ft.nc.shutdown_subjects == ["agent.job1.shutdown"]
    assert state.active is False
    assert state.adopted is False
    assert state.job_id is None
