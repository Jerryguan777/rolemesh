"""Per-coworker concurrent queue with three-level concurrency control.

Manages container processes across coworkers, handling message queueing,
task prioritization, retry with exponential backoff, and graceful shutdown.

Three levels: global + per-tenant + per-coworker.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rolemesh.core.config import IDLE_TIMEOUT
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.runtime import ContainerRuntime
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
    # ``active`` == a container exists for this group (spawn → exit), spanning
    # both processing and the warm idle window. ``processing`` == a turn is
    # in flight (turn-start → is_final); only this holds a turn slot, tracked
    # idempotently by ``slot_held`` (slot-follows-turn rework). A warm idle
    # container has ``active and not processing`` and holds no turn slot —
    # it only counts against the global live-container ceiling.
    active: bool = False
    processing: bool = False
    slot_held: bool = False
    last_active_at: float = 0.0  # monotonic ts of last is_final (warm-pool LRU key)
    idle_waiting: bool = False
    is_task_container: bool = False
    running_task_id: str | None = None
    pending_messages: bool = False
    pending_tasks: list[_QueuedTask] = field(default_factory=list)
    is_running: bool = False
    container_name: str | None = None
    group_folder: str | None = None
    job_id: str | None = None
    retry_count: int = 0
    tenant_id: str = ""
    coworker_id: str = ""
    # HITL approval (docs/12-hitl-approval-architecture.md §8). ``idle_handle`` is the
    # reaping-path-A timer, owned here (not in a main.py closure) so the
    # approval suspend path can cancel/re-arm it from a NATS handler.
    # ``awaiting_approval`` holds every request_id currently blocking the
    # container on a human decision — a ``set`` (not a bool) because one turn
    # can dispatch multiple gated tool calls concurrently. ``adopted`` marks a
    # state rebuilt by restart recovery for a container that has no
    # ``_run_for_group`` coroutine of its own.
    idle_handle: asyncio.TimerHandle | None = None
    awaiting_approval: set[str] = field(default_factory=set)
    adopted: bool = False
    # Reaper bookkeeping: consecutive sweeps this group was ``active`` with a
    # registered container that is no longer live. Reaped once it crosses the
    # two-sweep confirmation threshold (guards the just-exited race).
    missing_sweeps: int = 0


class GroupQueue:
    """Manages per-group container concurrency with three-level limits."""

    def __init__(
        self,
        transport: NatsTransport | None = None,
        runtime: ContainerRuntime | None = None,
        orchestrator_state: OrchestratorState | None = None,
        idle_timeout_ms: int = IDLE_TIMEOUT,
    ) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._idle_timeout_ms = idle_timeout_ms
        # Warm idle containers (``active and not processing``), ordered by last
        # is_final time → front is the LRU eviction victim when a cold start
        # needs room under the global live-container ceiling.
        self._warm: OrderedDict[str, float] = OrderedDict()
        self._waiting_groups: list[str] = []
        self._process_messages_fn: Callable[[str], Awaitable[bool]] | None = None
        self._on_queued: Callable[[str], Awaitable[None]] | None = None
        self._on_container_starting: Callable[[str], Awaitable[None]] | None = None
        self._shutting_down: bool = False
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._transport = transport
        self._runtime = runtime
        # Single source of truth for concurrency counters. A default is created
        # when none is injected (tests/eval) so all admission goes through one
        # path — no parallel in-queue counter to drift (the old ``_active_count``
        # is gone).
        if orchestrator_state is None:
            from rolemesh.core.orchestrator_state import OrchestratorState

            orchestrator_state = OrchestratorState()
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

    def _can_start_turn(self, tenant_id: str, coworker_id: str) -> bool:
        """Three-level turn admission (concurrent in-flight turns)."""
        return self._orch_state.can_start_container(tenant_id, coworker_id)

    def _can_spawn(self) -> bool:
        """Whether a new container fits under the global live-container ceiling."""
        return self._orch_state.can_spawn_container()

    def _acquire_turn(self, state: _GroupState) -> None:
        """Acquire a turn slot for ``state`` (idempotent on ``slot_held``)."""
        if state.slot_held:
            return
        state.slot_held = True
        self._orch_state.increment_active(state.tenant_id, state.coworker_id)

    def _release_turn(self, state: _GroupState) -> None:
        """Release ``state``'s turn slot (idempotent on ``slot_held``)."""
        if not state.slot_held:
            return
        state.slot_held = False
        self._orch_state.decrement_active(state.tenant_id, state.coworker_id)

    def _acquire_container(self, state: _GroupState) -> None:
        """Account a live container for ``state`` (idempotent on ``active``)."""
        if state.active:
            return
        state.active = True
        self._orch_state.acquire_container()

    def _release_container(self, state: _GroupState) -> None:
        """Account ``state``'s container exit (idempotent on ``active``).

        Warm-set removal is the caller's job (it holds the ``group_jid`` key);
        this only flips the flag and the global live counter.
        """
        if not state.active:
            return
        state.active = False
        self._orch_state.release_container()

    def _reserve_slot(self, state: _GroupState) -> None:
        """Synchronously reserve a live-container + turn slot at the admission
        *decision* point, before spawning the (async) runner.

        Closes a TOCTOU: ``_run_for_group`` / ``_run_task`` only increment the
        counters once their spawned task actually runs, so without reserving
        here, multiple check-then-spawn decisions in the same event-loop tick
        (notably one ``_drain_waiting`` pass releasing into several queued
        groups, but also a burst of ``enqueue_*`` calls) all observe the
        pre-increment counts, all pass, and over-admit — breaching the
        per-coworker / per-tenant / global ceilings until the turns settle.
        Bumping the counts synchronously here makes the next check in the same
        tick see them. Idempotent (``_acquire_*`` guard on ``slot_held`` /
        ``active``), so the runner's own acquire becomes a no-op.
        """
        self._acquire_container(state)
        state.processing = True
        self._acquire_turn(state)

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        """Set the callback for processing messages for a group."""
        self._process_messages_fn = fn

    def set_on_queued(self, fn: Callable[[str], Awaitable[None]]) -> None:
        """Callback invoked when a group enters the cross-group waiting queue.

        Fired only when the container can't start immediately due to
        concurrency limits, not when the group's own container is still
        running (that case is same-conversation follow-up, not queueing).
        """
        self._on_queued = fn

    def set_on_container_starting(self, fn: Callable[[str], Awaitable[None]]) -> None:
        """Callback invoked at the very start of _run_for_group.

        Represents the transition from "nothing running" to "container
        about to be provisioned." Called for both fresh starts and drains
        off the waiting queue.
        """
        self._on_container_starting = fn

    def _fire(self, cb: Callable[[str], Awaitable[None]] | None, group_jid: str) -> None:
        if cb is None:
            return
        coro = cb(group_jid)
        self._spawn(coro)

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

        # A container already exists (processing or warm). Mark the message
        # pending; delivery to a live container is the send_message (resume)
        # path, and a warm container that is never resumed drains into a fresh
        # container after it idle-reaps. No turn slot is taken here.
        if state.active:
            state.pending_messages = True
            logger.debug("Container active, message queued", group_jid=group_jid)
            return

        # Cold start needs BOTH turn admission and a free live-container slot.
        if not self._can_start_turn(state.tenant_id, state.coworker_id) or not self._can_spawn():
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            # If only the live-container ceiling blocks (turn admission is fine),
            # evict the LRU warm container to make room; its exit drains the
            # waiting queue (async eviction).
            if self._can_start_turn(state.tenant_id, state.coworker_id) and not self._can_spawn():
                self._evict_lru_warm()
            logger.debug(
                "At concurrency limit, message queued",
                group_jid=group_jid,
                global_active=self._orch_state.global_active,
                live_containers=self._orch_state.live_containers,
            )
            self._fire(self._on_queued, group_jid)
            return

        self._reserve_slot(state)
        self._spawn(self._run_for_group(group_jid, "messages"))

    def _evict_lru_warm(self) -> None:
        """Ask the least-recently-used warm container to wind down, freeing a
        live-container slot. Eviction is async: the victim's exit (R2) runs
        ``_drain_waiting`` which spawns the queued cold start.
        """
        for victim_jid in list(self._warm.keys()):
            vstate = self._groups.get(victim_jid)
            if vstate and vstate.active and not vstate.processing:
                logger.debug("Evicting LRU warm container", group_jid=victim_jid)
                self.request_shutdown(victim_jid)
                return

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
                self.request_shutdown(group_jid)
            logger.debug("Container active, task queued", group_jid=group_jid, task_id=task_id)
            return

        if not self._can_start_turn(state.tenant_id, state.coworker_id) or not self._can_spawn():
            state.pending_tasks.append(
                _QueuedTask(
                    id=task_id, group_jid=group_jid, fn=fn, tenant_id=state.tenant_id, coworker_id=state.coworker_id
                )
            )
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            if self._can_start_turn(state.tenant_id, state.coworker_id) and not self._can_spawn():
                self._evict_lru_warm()
            logger.debug(
                "At concurrency limit, task queued",
                group_jid=group_jid,
                task_id=task_id,
                global_active=self._orch_state.global_active,
                live_containers=self._orch_state.live_containers,
            )
            return

        # Run immediately
        task = _QueuedTask(
            id=task_id, group_jid=group_jid, fn=fn, tenant_id=state.tenant_id, coworker_id=state.coworker_id
        )
        self._reserve_slot(state)
        self._spawn(self._run_task(group_jid, task))

    def register_process(
        self,
        group_jid: str,
        container_name: str,
        group_folder: str | None = None,
        job_id: str | None = None,
    ) -> None:
        """Track an active container for a group."""
        state = self._get_group(group_jid)
        state.is_running = True
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder
        if job_id:
            state.job_id = job_id

    def get_active_container_name(self, group_jid: str) -> str | None:
        """Return the active container name for ``group_jid`` or ``None``.

        ``None`` is returned when the group has never had a container
        registered, OR when the container has exited and
        ``register_process`` left ``container_name`` cleared. Callers
        like the orchestrator-side ``web.run.cancel.*`` subscriber
        use this for the "stop the container if it is still alive"
        side of a cancel — the parallel ``terminate_run_via_user_cancel``
        UPDATE proceeds regardless of the return value, so a None
        here is not an error.
        """
        state = self._groups.get(group_jid)
        if state is None:
            return None
        return state.container_name

    def notify_idle(self, group_jid: str) -> None:
        """Turn complete (is_final): release the turn slot, keep the container warm.

        Slot-follows-turn rework — this is release-point R1. The turn slot is
        handed back here (not at container exit), so a warm idle container no
        longer occupies turn admission. The container itself stays alive in the
        warm pool until idle-reaped or LRU-evicted. Preempt (wind down) instead
        of going warm when tasks are pending.
        """
        state = self._get_group(group_jid)
        # An approval suspend explicitly disowns reaping path B (§8). If an
        # is_final output races in while a decision is still pending, honour the
        # suspend rather than releasing the slot / re-arming idle mid-approval.
        if state.awaiting_approval:
            return
        self._release_turn(state)
        state.processing = False
        state.idle_waiting = True
        state.last_active_at = time.monotonic()
        if state.active and not state.is_task_container:
            self._warm[group_jid] = state.last_active_at
            self._warm.move_to_end(group_jid)
        if state.pending_tasks:
            self.request_shutdown(group_jid)
        else:
            self.arm_idle_timer(group_jid)
        # A freed turn slot may admit a queued group.
        self._drain_waiting()

    # -- HITL approval: idle suspend / resume (docs/12-hitl-approval-architecture.md §8)

    def arm_idle_timer(self, group_jid: str) -> None:
        """(Re)arm the per-group idle reaping timer (reaping path A).

        Idle-timer ownership lives here, not in a ``main.py`` closure, because
        the approval suspend path must cancel and later re-arm this exact timer
        from a NATS handler that cannot reach a closure-local ``TimerHandle``.

        No-op while an approval is pending on this group: a bounded approval
        wait suspends reaping, and no path — not even a new follow-up message —
        may re-arm idle until the last decision resolves (§8 suspend step 6).
        The only caller that re-arms after a suspend is ``resume_from_approval``
        once the ``awaiting_approval`` set drains.
        """
        state = self._get_group(group_jid)
        if state.idle_handle is not None:
            state.idle_handle.cancel()
            state.idle_handle = None
        # No idle reaping while a turn is in flight (processing) or an approval
        # is pending. The idle window only governs the WARM phase; a long
        # processing turn that emits no output is bounded by the container
        # watchdog (TURN_INACTIVITY_TIMEOUT), not by idle reaping — so a low
        # IDLE_TIMEOUT can never kill a legitimately busy turn.
        if state.awaiting_approval or state.processing:
            return
        loop = asyncio.get_running_loop()
        cb = self._reap_adopted if state.adopted else self.request_shutdown
        state.idle_handle = loop.call_later(self._idle_timeout_ms / 1000.0, cb, group_jid)

    def cancel_idle_timer(self, group_jid: str) -> None:
        """Cancel the idle reaping timer for a group, if armed."""
        state = self._get_group(group_jid)
        if state.idle_handle is not None:
            state.idle_handle.cancel()
            state.idle_handle = None

    def suspend_for_approval(self, group_jid: str, request_id: str) -> None:
        """Suspend all three idle-reaping paths for a pending approval (§8).

        Closes path A (cancel the idle timer), paths B/C (force
        ``idle_waiting`` False so ``enqueue_task`` / ``notify_idle`` cannot
        request a shutdown), and records ``request_id`` in a ``set`` so
        concurrent approvals in one turn each hold the suspend independently.
        """
        state = self._get_group(group_jid)
        self.cancel_idle_timer(group_jid)
        state.idle_waiting = False
        # Defensive force-check: paths B/C are gated on this exact flag, so do
        # not rely on an implicit invariant that it was already False.
        assert state.idle_waiting is False
        state.awaiting_approval.add(request_id)

    def resume_from_approval(self, group_jid: str, request_id: str) -> bool:
        """Resume idle reaping when an approval resolves (§8). Idempotent.

        Double-cancel safe: a ``request_id`` not currently in the set (already
        resumed by an earlier decision, or never suspended here) is a no-op and
        returns ``False`` — so the S2 ``decision``-then-``cancel`` pair for one
        request cannot re-arm idle twice or mis-clear a sibling's suspend.
        Re-arms a full idle timeout only when the set drains, and only for a
        container the idle timer governs (not a scheduled-task container, whose
        own machinery reaps it).
        """
        state = self._get_group(group_jid)
        if request_id not in state.awaiting_approval:
            return False
        state.awaiting_approval.discard(request_id)
        if not state.awaiting_approval and not state.is_task_container:
            self.arm_idle_timer(group_jid)
        return True

    def is_awaiting_approval(self, group_jid: str) -> bool:
        """True while at least one approval is pending on this group."""
        state = self._groups.get(group_jid)
        return bool(state and state.awaiting_approval)

    def adopt_orphan_container(
        self,
        group_jid: str,
        *,
        job_id: str,
        tenant_id: str,
        coworker_id: str,
    ) -> None:
        """Re-register a container that outlived an orchestrator restart (R2).

        ``_groups`` is in-memory and empty after a restart, but a container
        blocked on a pending approval may still be alive. Rebuild just enough
        ``_GroupState`` for the suspend/resume + reaping machinery to manage it:
        mark it ``active`` with its ``job_id`` so ``request_shutdown`` can reach
        it, and flag it ``adopted`` so the idle reaper also tears the rebuilt
        state down — a normal container's teardown runs in ``_run_for_group``'s
        ``finally``, which is not executing for an adopted one.
        """
        state = self._get_group(group_jid)
        state.tenant_id = tenant_id or state.tenant_id
        state.coworker_id = coworker_id or state.coworker_id
        # Account the surviving container against the live ceiling. It is
        # idle-governed (not a turn): no turn slot, so ``processing`` stays
        # False and ``arm_idle_timer`` (via resume_from_approval) can reap it.
        self._acquire_container(state)
        state.processing = False
        state.is_running = True
        state.is_task_container = False
        state.adopted = True
        state.job_id = job_id

    def _reap_adopted(self, group_jid: str) -> None:
        """Idle-reap an adopted (restart-recovered) container and clear state.

        An adopted container has no ``_run_for_group`` coroutine whose
        ``finally`` resets the group and drains queued work, so after asking it
        to wind down we reset the state inline and drain. Otherwise the
        conversation would stay wedged ``active`` forever and never spawn a
        fresh container.
        """
        self.request_shutdown(group_jid)
        state = self._get_group(group_jid)
        self._release_turn(state)  # idempotent; adopted holds none, but be safe
        self._release_container(state)
        self._warm.pop(group_jid, None)
        state.processing = False
        state.is_running = False
        state.adopted = False
        state.container_name = None
        state.job_id = None
        state.idle_handle = None
        self._drain_group(group_jid)

    def send_message(self, group_jid: str, text: str) -> bool:
        """Deliver a message to this group's live container via NATS.

        Two cases (slot-follows-turn rework):

        * **processing** — the container is mid-turn; the message joins the
          in-flight batch (one is_final settles the whole batch), no new slot.
        * **warm** — idle between turns; this is a *resume* (acquire-point A2).
          It needs turn admission: if granted, acquire a turn slot, leave the
          warm pool, cancel idle reaping, then deliver; if denied, return False
          so the caller queues it for a later drain.

        Returns False when there is no live container, it is a task container,
        no transport is wired, or a warm resume is refused by admission.
        """
        state = self._get_group(group_jid)
        if not state.active or not state.job_id or state.is_task_container:
            return False

        if self._transport is None:
            return False

        if not state.processing:
            # Warm resume (A2): gate on turn admission before re-acquiring.
            if not self._can_start_turn(state.tenant_id, state.coworker_id):
                return False
            self.cancel_idle_timer(group_jid)
            self._warm.pop(group_jid, None)
            self._acquire_turn(state)
            state.processing = True
        state.idle_waiting = False

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

    def request_shutdown(self, group_jid: str) -> None:
        """Ask the active container to wind down, via NATS request-reply.

        Publishes a request on `agent.{job_id}.shutdown` and waits briefly for
        the agent's ack. The agent will finish its current turn (if any) and
        then exit. Fire-and-forget from the caller's perspective: the actual
        exit is asynchronous and may take seconds.

        Historical note: this used to be called `close_stdin` from the era
        when IPC ran over the container's stdin pipe. NATS replaced stdin
        long ago; the name is aligned with reality as of 2026-04-17.
        """
        state = self._get_group(group_jid)
        if not state.active or not state.job_id:
            return

        if self._transport is None:
            return

        # Capture job_id at call time: callers like the adopted-container reaper
        # reset state.job_id immediately after requesting shutdown, and this
        # send runs in a later loop turn — reading the attribute then would
        # target ``agent.None.shutdown``.
        job_id = state.job_id

        async def _send_shutdown() -> None:
            assert self._transport is not None
            try:
                await self._transport.nc.request(
                    f"agent.{job_id}.shutdown",
                    b"shutdown",
                    timeout=5.0,
                )
            except (OSError, TimeoutError):
                logger.debug("Shutdown request not acknowledged (agent may have exited)", group_jid=group_jid)

        task = asyncio.ensure_future(_send_shutdown())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def interrupt_current_turn(self, group_jid: str) -> None:
        """Abort the active container's current turn without closing it.

        Unlike request_shutdown (which signals container to exit), interrupt
        tells the agent to stop generating but keeps the container alive for
        subsequent prompts. Used by the web Stop button.
        """
        state = self._get_group(group_jid)
        if not state.active or not state.job_id:
            return

        if self._transport is None:
            return

        async def _send_interrupt() -> None:
            assert self._transport is not None
            assert state.job_id is not None
            try:
                # JetStream publish (fire-and-forget) — message is persisted
                # and delivered to the agent's JS consumer even if its event
                # loop is momentarily saturated by LLM streaming. Core NATS
                # request-reply previously raised NoRespondersError when the
                # agent's callback SUB hadn't fully flushed or was starved.
                #
                # Intentionally asymmetric vs request_shutdown (which uses
                # nc.request with timeout=5.0): interrupt is best-effort by
                # nature — cancel propagation through the SDK takes time no
                # matter what, and the orchestrator observes completion via
                # the StoppedEvent on the result stream rather than waiting
                # for a synchronous ack here.
                await self._transport.js.publish(
                    f"agent.{state.job_id}.interrupt",
                    b"interrupt",
                )
            except (OSError, TimeoutError):
                logger.debug(
                    "Interrupt publish failed (agent may have exited)",
                    group_jid=group_jid,
                )

        task = asyncio.ensure_future(_send_interrupt())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get_group(group_jid)
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False
        self.cancel_idle_timer(group_jid)
        # Cold start: acquire a live container AND the first turn slot. Within
        # this single coroutine the turn slot may be released (notify_idle, R1)
        # and re-acquired (send_message resume, A2) many times as the warm
        # container handles follow-ups; the finally below is the idempotent
        # backstop (R2) for a container that exits mid-turn.
        self._acquire_container(state)
        state.processing = True
        self._acquire_turn(state)

        logger.debug(
            "Starting container for group",
            group_jid=group_jid,
            reason=reason,
            global_active=self._orch_state.global_active,
            live_containers=self._orch_state.live_containers,
        )
        self._fire(self._on_container_starting, group_jid)

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
            self._release_turn(state)  # R2: idempotent (R1 usually released already)
            self._release_container(state)
            self._warm.pop(group_jid, None)
            state.processing = False
            state.is_running = False
            state.container_name = None
            state.group_folder = None
            state.job_id = None
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        state = self._get_group(group_jid)
        state.idle_waiting = False
        state.is_task_container = True
        state.running_task_id = task.id
        self.cancel_idle_timer(group_jid)
        # Tasks are one-shot (no warm reuse): hold both a live container and a
        # turn slot for the whole task, release both on exit.
        self._acquire_container(state)
        state.processing = True
        self._acquire_turn(state)

        logger.debug(
            "Running queued task",
            group_jid=group_jid,
            task_id=task.id,
            global_active=self._orch_state.global_active,
            live_containers=self._orch_state.live_containers,
        )

        try:
            await task.fn()
        except Exception:
            logger.exception("Error running task", group_jid=group_jid, task_id=task.id)
        finally:
            self._release_turn(state)
            self._release_container(state)
            self._warm.pop(group_jid, None)
            state.processing = False
            state.is_task_container = False
            state.running_task_id = None
            state.is_running = False
            state.container_name = None
            state.group_folder = None
            state.job_id = None
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

        # _drain_group runs after this group's container has exited (R2) — so
        # restarting its own pending work is a cold start: gate on turn
        # admission AND a free live-container slot. If either is unavailable,
        # queue it for a later cross-group drain rather than bypassing limits.
        if state.pending_tasks or state.pending_messages:
            if self._can_start_turn(state.tenant_id, state.coworker_id) and self._can_spawn():
                self._reserve_slot(state)
                if state.pending_tasks:
                    task = state.pending_tasks.pop(0)
                    self._spawn(self._run_task(group_jid, task))
                    return
                self._spawn(self._run_for_group(group_jid, "drain"))
                return
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)

        self._drain_waiting()

    def _drain_waiting(self) -> None:
        remaining: list[str] = []
        for next_jid in self._waiting_groups:
            state = self._get_group(next_jid)
            if state.active:
                # Already has a container (it will self-drain); drop from waiting.
                continue
            if not (self._can_start_turn(state.tenant_id, state.coworker_id) and self._can_spawn()):
                remaining.append(next_jid)
                continue

            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                self._reserve_slot(state)  # bump counts NOW so the next iteration sees them
                self._spawn(self._run_task(next_jid, task))
            elif state.pending_messages:
                self._reserve_slot(state)  # bump counts NOW so the next iteration sees them
                self._spawn(self._run_for_group(next_jid, "drain"))
            else:
                continue  # nothing to do, don't re-add
        self._waiting_groups = remaining

    # -- out-of-band reaper (counter reconciliation) ---------------------

    async def reconcile_once(self, prefix: str = "rolemesh-") -> int:
        """One reaper sweep: reap groups marked ``active`` whose container is no
        longer live, reconciling the three-level + live counters with reality.

        This is the backstop for a leaked slot — a ``_run_for_group`` whose
        ``finally`` never ran (abnormal coroutine death) leaves ``active`` True
        and the counters incremented while the container is already gone. We
        confirm a group "ghost" across two consecutive sweeps (the just-exited
        race window where the container died but the finally hasn't run yet) and
        a group whose container is not yet registered is given grace. A
        ``list_live`` failure aborts the sweep so a transient runtime hiccup
        never mass-reaps healthy groups. Returns the number reaped.
        """
        if self._shutting_down or self._runtime is None:
            return 0
        try:
            live = await self._runtime.list_live(prefix)
        except (OSError, RuntimeError):
            logger.debug("reaper: list_live failed; skipping sweep")
            return 0

        reaped = 0
        for group_jid, state in list(self._groups.items()):
            if not state.active:
                state.missing_sweeps = 0
                continue
            if state.container_name and state.container_name in live:
                state.missing_sweeps = 0  # healthy
                continue
            if state.container_name is None:
                continue  # just spawned, not yet registered — grace
            # Registered but absent from the live set — confirm across two sweeps.
            state.missing_sweeps += 1
            if state.missing_sweeps < 2:
                continue
            logger.warning(
                "reaper: reaping ghost group (container gone, slot leaked)",
                group_jid=group_jid,
                container=state.container_name,
            )
            self._reap_ghost(group_jid)
            reaped += 1
        return reaped

    def _reap_ghost(self, group_jid: str) -> None:
        """Release a ghost group's slot + container counters and drain its work."""
        state = self._get_group(group_jid)
        self._release_turn(state)
        self._release_container(state)
        self._warm.pop(group_jid, None)
        self.cancel_idle_timer(group_jid)
        state.processing = False
        state.is_running = False
        state.is_task_container = False
        state.running_task_id = None
        state.container_name = None
        state.job_id = None
        state.missing_sweeps = 0
        self._drain_group(group_jid)

    async def run_reaper(self, interval_s: float = 60.0, prefix: str = "rolemesh-") -> None:
        """Periodic reconciliation loop (started by the orchestrator lifespan)."""
        while not self._shutting_down:
            await asyncio.sleep(interval_s)
            try:
                n = await self.reconcile_once(prefix)
                if n:
                    logger.info("reaper swept ghost containers", reaped=n)
            except Exception:
                logger.exception("reaper sweep failed")

    async def shutdown(self, _grace_period_ms: int = 0) -> None:
        """Graceful shutdown -- detach containers, don't kill them."""
        self._shutting_down = True

        active_containers: list[str] = []
        for state in self._groups.values():
            if state.is_running and state.container_name:
                active_containers.append(state.container_name)

        logger.info(
            "GroupQueue shutting down (containers detached, not killed)",
            live_containers=self._orch_state.live_containers,
            detached_containers=active_containers,
        )

        if self._runtime:
            for name in active_containers:
                try:
                    await self._runtime.stop(name)
                except (OSError, RuntimeError):
                    logger.debug("Failed to stop container during shutdown", container=name)
