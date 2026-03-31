"""Per-coworker concurrent queue with three-level concurrency control.

Manages container processes across coworkers, handling message queueing,
task prioritization, retry with exponential backoff, and graceful shutdown.

Three levels: global + per-tenant + per-coworker.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerHandle, ContainerRuntime
    from rolemesh.core.orchestrator_state import OrchestratorState
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()

_MAX_RETRIES = 5
_BASE_RETRY_MS = 5000


@dataclass
class _QueuedTask:
    id: str
    group_jid: str
    fn: Callable[[], Awaitable[None]]
    tenant_id: str = ""
    coworker_id: str = ""


@dataclass
class _GroupState:
    active: bool = False
    idle_waiting: bool = False
    is_task_container: bool = False
    running_task_id: str | None = None
    pending_messages: bool = False
    pending_tasks: list[_QueuedTask] = field(default_factory=list)
    process: ContainerHandle | None = None
    container_name: str | None = None
    group_folder: str | None = None
    job_id: str | None = None
    retry_count: int = 0
    tenant_id: str = ""
    coworker_id: str = ""


class GroupQueue:
    """Manages per-group container concurrency with three-level limits."""

    def __init__(
        self,
        transport: NatsTransport | None = None,
        runtime: ContainerRuntime | None = None,
        orchestrator_state: OrchestratorState | None = None,
    ) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._active_count: int = 0
        self._waiting_groups: list[str] = []
        self._process_messages_fn: Callable[[str], Awaitable[bool]] | None = None
        self._shutting_down: bool = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._transport = transport
        self._runtime = runtime
        self._orch_state = orchestrator_state

    def _spawn(self, coro: Awaitable[None]) -> None:
        """Launch a background task and track it to prevent GC."""
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _get_group(self, group_jid: str) -> _GroupState:
        state = self._groups.get(group_jid)
        if state is None:
            state = _GroupState()
            self._groups[group_jid] = state
        return state

    def _can_start(self, tenant_id: str, coworker_id: str) -> bool:
        """Check three-level concurrency."""
        if self._orch_state:
            return self._orch_state.can_start_container(tenant_id, coworker_id)
        # Fallback to simple global limit
        from rolemesh.core.config import MAX_CONCURRENT_CONTAINERS

        return self._active_count < MAX_CONCURRENT_CONTAINERS

    def _increment(self, tenant_id: str, coworker_id: str) -> None:
        self._active_count += 1
        if self._orch_state:
            self._orch_state.increment_active(tenant_id, coworker_id)

    def _decrement(self, tenant_id: str, coworker_id: str) -> None:
        self._active_count -= 1
        if self._orch_state:
            self._orch_state.decrement_active(tenant_id, coworker_id)

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        """Set the callback for processing messages for a group."""
        self._process_messages_fn = fn

    def enqueue_message_check(
        self,
        group_jid: str,
        tenant_id: str = "",
        coworker_id: str = "",
    ) -> None:
        """Queue a message check for a group."""
        if self._shutting_down:
            return

        state = self._get_group(group_jid)
        state.tenant_id = tenant_id or state.tenant_id
        state.coworker_id = coworker_id or state.coworker_id

        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", group_jid=group_jid)
            return

        if not self._can_start(state.tenant_id, state.coworker_id):
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit, message queued",
                group_jid=group_jid,
                active_count=self._active_count,
            )
            return

        self._spawn(self._run_for_group(group_jid, "messages"))

    def enqueue_task(
        self,
        group_jid: str,
        task_id: str,
        fn: Callable[[], Awaitable[None]],
        tenant_id: str = "",
        coworker_id: str = "",
    ) -> None:
        """Queue a task for execution."""
        if self._shutting_down:
            return

        state = self._get_group(group_jid)
        state.tenant_id = tenant_id or state.tenant_id
        state.coworker_id = coworker_id or state.coworker_id

        # Prevent double-queuing
        if state.running_task_id == task_id:
            logger.debug("Task already running, skipping", group_jid=group_jid, task_id=task_id)
            return
        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug("Task already queued, skipping", group_jid=group_jid, task_id=task_id)
            return

        if state.active:
            state.pending_tasks.append(
                _QueuedTask(
                    id=task_id, group_jid=group_jid, fn=fn, tenant_id=state.tenant_id, coworker_id=state.coworker_id
                )
            )
            if state.idle_waiting:
                self.close_stdin(group_jid)
            logger.debug("Container active, task queued", group_jid=group_jid, task_id=task_id)
            return

        if not self._can_start(state.tenant_id, state.coworker_id):
            state.pending_tasks.append(
                _QueuedTask(
                    id=task_id, group_jid=group_jid, fn=fn, tenant_id=state.tenant_id, coworker_id=state.coworker_id
                )
            )
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                "At concurrency limit, task queued",
                group_jid=group_jid,
                task_id=task_id,
                active_count=self._active_count,
            )
            return

        # Run immediately
        task = _QueuedTask(
            id=task_id, group_jid=group_jid, fn=fn, tenant_id=state.tenant_id, coworker_id=state.coworker_id
        )
        self._spawn(self._run_task(group_jid, task))

    def register_process(
        self,
        group_jid: str,
        proc: ContainerHandle,
        container_name: str,
        group_folder: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Track an active container handle for a group."""
        state = self._get_group(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder
        if job_id:
            state.job_id = job_id

    def notify_idle(self, group_jid: str) -> None:
        """Mark container as idle-waiting. Preempt if tasks pending."""
        state = self._get_group(group_jid)
        state.idle_waiting = True
        if state.pending_tasks:
            self.close_stdin(group_jid)

    def send_message(self, group_jid: str, text: str) -> bool:
        """Send a follow-up message to the active container via NATS JetStream."""
        state = self._get_group(group_jid)
        if not state.active or not state.job_id or state.is_task_container:
            return False
        state.idle_waiting = False

        if self._transport is None:
            return False

        try:

            async def _send() -> None:
                assert self._transport is not None
                assert state.job_id is not None
                await self._transport.js.publish(
                    f"agent.{state.job_id}.input",
                    json.dumps({"type": "message", "text": text}).encode(),
                )

            bg = asyncio.ensure_future(_send())
            self._background_tasks.add(bg)
            bg.add_done_callback(self._background_tasks.discard)
            return True
        except (OSError, RuntimeError):
            logger.exception("Failed to send follow-up message via NATS", group_jid=group_jid)
            return False

    def close_stdin(self, group_jid: str) -> None:
        """Signal the active container to wind down via NATS request-reply."""
        state = self._get_group(group_jid)
        if not state.active or not state.job_id:
            return

        if self._transport is None:
            return

        async def _send_close() -> None:
            assert self._transport is not None
            assert state.job_id is not None
            try:
                await self._transport.nc.request(
                    f"agent.{state.job_id}.close",
                    b"close",
                    timeout=5.0,
                )
            except (OSError, TimeoutError):
                logger.debug("Close signal not acknowledged (agent may have exited)", group_jid=group_jid)

        task = asyncio.ensure_future(_send_close())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False
        self._increment(state.tenant_id, state.coworker_id)

        logger.debug(
            "Starting container for group",
            group_jid=group_jid,
            reason=reason,
            active_count=self._active_count,
        )

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except Exception:
            logger.exception("Error processing messages for group", group_jid=group_jid)
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            state.job_id = None
            self._decrement(state.tenant_id, state.coworker_id)
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = True
        state.running_task_id = task.id
        self._increment(state.tenant_id, state.coworker_id)

        logger.debug(
            "Running queued task",
            group_jid=group_jid,
            task_id=task.id,
            active_count=self._active_count,
        )

        try:
            await task.fn()
        except Exception:
            logger.exception("Error running task", group_jid=group_jid, task_id=task.id)
        finally:
            state.active = False
            state.is_task_container = False
            state.running_task_id = None
            state.process = None
            state.container_name = None
            state.group_folder = None
            state.job_id = None
            self._decrement(state.tenant_id, state.coworker_id)
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: _GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > _MAX_RETRIES:
            logger.error(
                "Max retries exceeded, dropping messages",
                group_jid=group_jid,
                retry_count=state.retry_count,
            )
            state.retry_count = 0
            return

        delay_s = (_BASE_RETRY_MS * math.pow(2, state.retry_count - 1)) / 1000.0
        logger.info(
            "Scheduling retry with backoff",
            group_jid=group_jid,
            retry_count=state.retry_count,
            delay_s=delay_s,
        )

        async def _retry() -> None:
            await asyncio.sleep(delay_s)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid, state.tenant_id, state.coworker_id)

        self._spawn(_retry())

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # Tasks first
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            self._spawn(self._run_task(group_jid, task))
            return

        if state.pending_messages:
            self._spawn(self._run_for_group(group_jid, "drain"))
            return

        self._drain_waiting()

    def _drain_waiting(self) -> None:
        remaining: list[str] = []
        for next_jid in self._waiting_groups:
            state = self._get_group(next_jid)
            if not self._can_start(state.tenant_id, state.coworker_id):
                remaining.append(next_jid)
                continue

            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                self._spawn(self._run_task(next_jid, task))
            elif state.pending_messages:
                self._spawn(self._run_for_group(next_jid, "drain"))
            else:
                continue  # nothing to do, don't re-add
        self._waiting_groups = remaining

    async def shutdown(self, _grace_period_ms: int = 0) -> None:
        """Graceful shutdown -- detach containers, don't kill them."""
        self._shutting_down = True

        active_containers: list[str] = []
        for state in self._groups.values():
            if state.process and state.container_name:
                active_containers.append(state.container_name)

        logger.info(
            "GroupQueue shutting down (containers detached, not killed)",
            active_count=self._active_count,
            detached_containers=active_containers,
        )

        if self._runtime:
            for name in active_containers:
                try:
                    await self._runtime.stop(name)
                except (OSError, RuntimeError):
                    logger.debug("Failed to stop container during shutdown", container=name)
