"""Scheduled task execution (cron, interval, once).

Runs a polling loop that picks up due tasks and enqueues them for
container execution via the GroupQueue.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from croniter import croniter

from rolemesh.agent import AgentInput, AgentOutput
from rolemesh.container.runner import write_tasks_snapshot
from rolemesh.core.config import SCHEDULER_POLL_INTERVAL
from rolemesh.core.logger import get_logger
from rolemesh.core.types import ScheduledTask, TaskRunLog
from rolemesh.db import (
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_task_after_run,
)

if TYPE_CHECKING:
    from rolemesh.agent.container_executor import ContainerAgentExecutor
    from rolemesh.container.scheduler import GroupQueue
    from rolemesh.core.orchestrator_state import OrchestratorState
    from rolemesh.core.types import Coworker
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


def compute_next_run(task: ScheduledTask) -> str | None:
    """Compute the next run time for a recurring task."""
    if task.schedule_type == "once":
        return None

    now = time.time()

    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value, datetime.now(UTC))
        return cron.get_next(datetime).isoformat()

    if task.schedule_type == "interval":
        try:
            ms = int(task.schedule_value)
        except (ValueError, TypeError):
            ms = 0

        if ms <= 0:
            logger.warning("Invalid interval value", task_id=task.id, value=task.schedule_value)
            return datetime.fromtimestamp(now + 60.0, tz=UTC).isoformat()

        assert task.next_run is not None
        next_ts = datetime.fromisoformat(task.next_run).timestamp() + ms / 1000.0
        while next_ts <= now:
            next_ts += ms / 1000.0
        return datetime.fromtimestamp(next_ts, tz=UTC).isoformat()

    return None


class SchedulerDependencies(Protocol):
    """Dependencies injected into the scheduler loop."""

    @property
    def orchestrator_state(self) -> OrchestratorState: ...
    def get_coworker(self, coworker_id: str) -> Coworker | None: ...
    def get_session(self, conversation_id: str) -> str | None: ...
    @property
    def queue(self) -> GroupQueue: ...
    def on_process(
        self,
        group_jid: str,
        container_name: str,
        group_folder: str,
        job_id: str | None = None,
    ) -> None: ...
    def send_message(self, jid: str, text: str, coworker_id: str = "") -> Awaitable[None]: ...
    @property
    def transport(self) -> NatsTransport | None: ...
    @property
    def executor(self) -> ContainerAgentExecutor | None: ...
    def get_executor(self, backend_name: str) -> ContainerAgentExecutor | None: ...


_TASK_CLOSE_DELAY_S: float = 10.0


def _compute_queue_key(task: ScheduledTask) -> str:
    """Return the ``GroupQueue`` key for a scheduled task.

    Must match what ``main.py`` uses for ``enqueue_message_check`` /
    ``notify_idle`` / ``request_shutdown`` so the same conversation
    ends up in the same ``_GroupState`` entry. The initial commit
    (38e79d8) keyed everything on ``chat_jid``; the multi-tenant
    refactor (058eb10) migrated the messaging side to
    ``conversation_id`` but accidentally left the scheduler keyed
    on ``channel_chat_id``, splitting one conversation into two
    state entries and breaking warm-container preemption.

    Falls back to ``coworker_id`` only when no conversation is bound
    — that's the legitimate "coworker-scoped reminder" shape and
    keeps such tasks queued under a stable identifier.
    """
    return task.conversation_id or task.coworker_id


async def _run_task(
    task: ScheduledTask,
    deps: SchedulerDependencies,
) -> None:
    """Run a single scheduled task in a container."""
    start_time = time.monotonic()

    coworker = deps.get_coworker(task.coworker_id)
    if coworker is None:
        logger.error("Coworker not found for task", task_id=task.id, coworker_id=task.coworker_id)
        await log_task_run(
            TaskRunLog(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=int((time.monotonic() - start_time) * 1000),
                status="error",
                result=None,
                error=f"Coworker not found: {task.coworker_id}",
            )
        )
        return

    logger.info("Running scheduled task", task_id=task.id, coworker=coworker.name)

    executor = deps.get_executor(coworker.agent_backend) or deps.executor
    if executor is None:
        logger.error("Agent executor not available for task", task_id=task.id)
        await log_task_run(
            TaskRunLog(
                tenant_id=task.tenant_id,
                task_id=task.id,
                run_at=datetime.now(UTC).isoformat(),
                duration_ms=int((time.monotonic() - start_time) * 1000),
                status="error",
                result=None,
                error="Agent executor not initialized",
            )
        )
        return

    permissions = coworker.permissions
    transport = deps.transport

    # Two derived values share an origin (the bound Conversation) but
    # are used for different things:
    #   queue_key — identity inside ``GroupQueue._groups``. Must equal
    #     ``main.py``'s message-side key (``conversation_id``) so a
    #     warm message container and an incoming scheduled task share
    #     the same ``_GroupState``. See ``_compute_queue_key`` for the
    #     058eb10 regression backstory.
    #   chat_jid — destination address for the channel gateway when
    #     the task's reply goes out (Telegram chat id, web chat uuid,
    #     ...). Comes from ``Conversation.channel_chat_id``; stays
    #     channel-specific.
    conversation_id = task.conversation_id or ""
    queue_key = _compute_queue_key(task)
    chat_jid = ""

    # Find the coworker state for context
    orch = deps.orchestrator_state
    cw_state = orch.coworkers.get(coworker.id)
    if cw_state and conversation_id:
        for conv in cw_state.conversations.values():
            if conv.conversation.id == conversation_id:
                chat_jid = conv.conversation.channel_chat_id
                break

    if transport is not None:
        all_tasks = await get_all_tasks(task.tenant_id)
        await write_tasks_snapshot(
            transport,
            task.tenant_id,
            coworker.folder,
            permissions=permissions,
            tasks=[
                {
                    "id": t.id,
                    "coworkerFolder": coworker.folder,
                    "prompt": t.prompt,
                    "schedule_type": t.schedule_type,
                    "schedule_value": t.schedule_value,
                    "status": t.status,
                    "next_run": t.next_run,
                }
                for t in all_tasks
            ],
        )

    result: str | None = None
    error: str | None = None

    session_id = deps.get_session(conversation_id) if task.context_mode == "group" and conversation_id else None

    close_handle: asyncio.TimerHandle | None = None

    def _schedule_close() -> None:
        nonlocal close_handle
        if close_handle is not None:
            return
        loop = asyncio.get_running_loop()
        close_handle = loop.call_later(
            _TASK_CLOSE_DELAY_S,
            lambda: deps.queue.request_shutdown(queue_key),
        )

    try:

        async def _on_output(streamed_output: AgentOutput) -> None:
            nonlocal result, error
            if streamed_output.result:
                result = streamed_output.result
                if chat_jid:
                    await deps.send_message(chat_jid, streamed_output.result, coworker_id=task.coworker_id)
                _schedule_close()
            if streamed_output.status == "success":
                # Only release idle-gating once the run_prompt batch settles.
                # With the is_final contract, per-prompt replies arrive as
                # is_final=False and must not fire notify_idle — otherwise a
                # concurrently pending task on the same queue_key would preempt
                # (request_shutdown) the container mid-batch.
                if streamed_output.is_final:
                    deps.queue.notify_idle(queue_key)
                _schedule_close()
            if streamed_output.status == "error":
                error = streamed_output.error or "Unknown error"

        output = await executor.execute(
            AgentInput(
                prompt=task.prompt,
                session_id=session_id,
                group_folder=coworker.folder,
                chat_jid=chat_jid,
                permissions=permissions.to_dict(),
                is_scheduled_task=True,
                assistant_name=coworker.name,
                tenant_id=task.tenant_id,
                coworker_id=task.coworker_id,
                conversation_id=conversation_id,
                # v6.1 §P1.7 — recover the creator identity onto the
                # turn. NULL ``created_by_user_id`` falls back to the
                # empty default.
                user_id=task.created_by_user_id or "",
            ),
            lambda container_name, job_id: deps.on_process(
                queue_key, container_name, coworker.folder, job_id
            ),
            _on_output,
        )

        if close_handle is not None:
            close_handle.cancel()

        if output.status == "error":
            error = output.error or "Unknown error"
        elif output.result:
            result = output.result

        logger.info("Task completed", task_id=task.id, duration_ms=int((time.monotonic() - start_time) * 1000))
    except (OSError, RuntimeError, ValueError) as exc:
        if close_handle is not None:
            close_handle.cancel()
        error = str(exc)
        logger.error("Task failed", task_id=task.id, error=error)

    duration_ms = int((time.monotonic() - start_time) * 1000)

    await log_task_run(
        TaskRunLog(
            tenant_id=task.tenant_id,
            task_id=task.id,
            run_at=datetime.now(UTC).isoformat(),
            duration_ms=duration_ms,
            status="error" if error else "success",
            result=result,
            error=error,
        )
    )

    next_run = compute_next_run(task)
    result_summary: str
    if error:
        result_summary = f"Error: {error}"
    elif result:
        result_summary = result[:200]
    else:
        result_summary = "Completed"
    await update_task_after_run(task.id, next_run, result_summary, tenant_id=task.tenant_id)


_scheduler_running: bool = False


async def _enqueue_due_tasks(
    due_tasks: list[ScheduledTask], deps: SchedulerDependencies
) -> None:
    """Push one tick worth of due tasks into the ``GroupQueue``.

    Extracted from ``_loop`` so the queue-key contract
    (``_compute_queue_key``) can be exercised against a real DB row
    without spinning up the infinite scheduler poll loop.

    Re-fetches each row's current status before enqueueing — between
    ``get_due_tasks`` and this call the row may have been cancelled
    or completed by another process. Skipping the stale ones keeps
    a status race from firing an already-cancelled reminder.
    """
    for task in due_tasks:
        current_task = await get_task_by_id(task.id, tenant_id=task.tenant_id)
        if not current_task or current_task.status != "active":
            continue

        def _make_fn(t: ScheduledTask) -> Callable[[], Awaitable[None]]:
            async def _task_fn() -> None:
                await _run_task(t, deps)

            return _task_fn

        deps.queue.enqueue_task(
            _compute_queue_key(current_task),
            current_task.id,
            _make_fn(current_task),
            tenant_id=current_task.tenant_id,
            coworker_id=current_task.coworker_id,
        )


def start_scheduler_loop(deps: SchedulerDependencies) -> asyncio.Task[None]:
    """Launch an asyncio task that polls for due scheduled tasks."""
    global _scheduler_running
    if _scheduler_running:
        logger.debug("Scheduler loop already running, skipping duplicate start")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        fut.set_result(None)
        return asyncio.ensure_future(fut)
    _scheduler_running = True
    logger.info("Scheduler loop started")

    async def _loop() -> None:
        while True:
            try:
                due_tasks = await get_due_tasks()
                if due_tasks:
                    logger.info("Found due tasks", count=len(due_tasks))
                await _enqueue_due_tasks(due_tasks, deps)
            except (OSError, RuntimeError, ValueError):
                logger.exception("Error in scheduler loop")

            await asyncio.sleep(SCHEDULER_POLL_INTERVAL)

    return asyncio.create_task(_loop())


def _reset_scheduler_loop_for_tests() -> None:
    """Reset module state for tests."""
    global _scheduler_running
    _scheduler_running = False
