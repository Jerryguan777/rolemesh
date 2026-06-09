"""Frontdesk v1.2 delegation handler — orchestrator side.

Implements the core RPC responder for ``agent.*.delegate.request`` (handbook
§6 Step 5). The handler runs the target coworker in a child
conversation, synchronously awaits the terminal event, and ships the
result back to the calling frontdesk over core NATS request-reply.

Eight non-negotiable contracts live in this file's shape, not just in
its docstring. They are pinned by comments and by the 23-scenario test
matrix in handbook §6 Step 5.5:

(a) **Pi backend two-event success**: a turn is split into a
    text-bearing event (``status='success', is_final=False``,
    ``result=<text>``) and a marker (``status='success', is_final=True,
    result=None``). Claude backend emits one event. ``_on_output`` tracks
    both kinds separately; ``_merge_pi_two_event_pattern`` merges. A
    later terminal event (error / safety_blocked / stopped) ALWAYS wins
    over a previously-seen success marker — see test 13c.

(b) **Container does NOT self-exit**: every terminal path in
    ``_on_output`` (success-marker, error, safety_blocked, stopped) and
    the business-timeout path in ``_closure`` MUST call
    ``queue.request_shutdown(queue_key)`` explicitly. Missing the call
    on any one path strands the container until ``CONTAINER_TIMEOUT``
    (30 min idle). Test 21 enforces this on every path.

(c) **OUTER_GUARD vs business deadline are orthogonal**:
    ``OUTER_GUARD_S`` (30s) is for "the closure never ran" only. The
    business deadline (``DEFAULT_BUSINESS_DEADLINE_S`` = 300s for slow
    LLMs) lives INSIDE the closure as ``wait_for(execute, 300)``. The
    timers do NOT stack and produce DISTINCT audit messages so ops can
    distinguish them.

    Implementation note: a literal ``wait_for(result_future, 30s)``
    around the entire delegation (as the handbook §6 Step 5.3 sketch
    suggests) would conflate the two — a 200s LLM would trip the
    30s outer guard and surface as "queue stalled" instead of as a
    business timeout. We split into ``started_future`` (set when
    ``_closure`` actually begins) for the OUTER_GUARD, and
    ``result_future`` (set on terminal event or closure completion)
    for the unbounded final wait. Audit messages stay distinct, and
    the 30s outer guard truly only catches "closure never ran".

(d) **Sticky session persistence is explicit**: after a sticky success
    we explicitly ``set_session(child_conv.id, ..., new_session_id)``.
    The orchestrator's ``_run_agent`` set_session path does NOT cover
    delegation (it's a sidepath). DB failure here is non-fatal: log a
    warning, return the success response anyway; the next sticky call
    cold-starts the session.

(e) **Catalog renders ``(id: <folder>)`` and the system prompt uses
    "agent id"** (handbook §4 #16). ``_resolve_target`` therefore tries
    matching by folder first, then falls back to id (UUID) — because
    the literal "id:" label in the catalog occasionally nudges a model
    to pass the UUID rather than the folder slug.

(f) ``MAX_DELEGATION_DEPTH = 1``: payload ``depth=0`` (frontdesk's
    initial call) is allowed, ``depth=1`` (caller is already a delegate)
    is refused. Strictly one hop.

(g) **Sticky concurrency**: two concurrent sticky calls for the same
    ``(parent, target)`` pair must converge to a single child conv row
    and a single shared session. The ``INSERT … ON CONFLICT DO NOTHING
    RETURNING`` + fallback SELECT in ``db.delegation.create_child_conversation``
    is the mechanism; this handler does not need to coordinate.

(h) **Queue-shutdown refusal is pre-enqueue**: if
    ``queue._shutting_down`` is True at the time we'd call
    ``enqueue_task``, we MUST write the audit row with ``status='error'``
    and the literal message ``'GroupQueue is shutting down; delegation
    refused.'``, then return. Calling ``enqueue_task`` first and trying
    to recover would silently drop the task (scheduler.py:181-182).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from rolemesh.agent.executor import PROGRESS_STATUSES, AgentInput, AgentOutput
from rolemesh.core.logger import get_logger
from rolemesh.db.chat import get_session, set_session
from rolemesh.db.delegation import (
    create_child_conversation,
    find_child_conversation,
    get_or_create_internal_binding,
    insert_delegation,
    update_delegation_terminal,
)
from rolemesh.orchestration._chip_throttle import ChipThrottleBucket
from rolemesh.orchestration.catalog import render_agent_catalog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.scheduler import GroupQueue
    from rolemesh.core.orchestrator_state import OrchestratorState
    from rolemesh.core.types import Coworker


logger = get_logger()


# ---------------------------------------------------------------------------
# Constants — see contracts (c), (f).
# ---------------------------------------------------------------------------

# Maximum hops permitted. Frontdesk → specialist is the only allowed
# shape (depth=0 → depth=1). Specialist → another specialist (depth=1
# → would be 2) is rejected. Two defence layers: (1) the domain agent
# default permission ``agent_delegate=False`` blocks at the agent-side
# tool gate; (2) this constant blocks server-side regardless.
MAX_DELEGATION_DEPTH = 1

# Business deadline. 300s comfortably covers slow LLMs; approval queues
# are async (submit_proposal fire-and-forget) and do NOT block on this.
DEFAULT_BUSINESS_DEADLINE_S = 300.0

# Outer-guard timeout: covers "closure was enqueued but never started"
# (queue stalled, event loop wedged, etc.). Does NOT cover slow LLMs;
# those run inside the closure under the business deadline. See
# contract (c) for the rationale on splitting started_future from
# result_future.
OUTER_GUARD_S = 30.0


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def handle_delegate_request(
    msg: object,
    *,
    state: OrchestratorState,
    queue: GroupQueue,
    get_executor: Callable[[str], Any],
    emit_chip_event: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Core NATS responder for ``agent.*.delegate.request``.

    Always replies (success or error), never raises out. Errors are
    routed through ``_process_one``, which writes an audit row before
    returning the wire payload so ops have a single source of truth
    even when the response payload is dropped in transit.

    ``emit_chip_event`` is the frontdesk v1.5 hook for surfacing
    target progress to the parent web UI as a sub-chip. When None,
    chip events are silently skipped (audit path untouched).
    """
    try:
        response = await _process_one(
            msg, state, queue, get_executor, emit_chip_event,
        )
    except Exception as exc:
        # Top-of-handler safety net: a raise here would silently break
        # the delegation responder for every subsequent request until
        # the subscription is rewired. Audit rows are written inside
        # ``_process_one`` for the in-flight delegation; this branch
        # only fires for failures BEFORE we got far enough to write
        # one (e.g. payload JSON parse error). logger.exception is the
        # ruff-sanctioned blind-except pattern (captures the traceback).
        logger.exception("delegate handler crashed", error=str(exc))
        response = _err(f"Delegation handler error: {exc}")
    body = json.dumps(response).encode("utf-8")
    with contextlib.suppress(Exception):
        await msg.respond(body)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------


async def _process_one(
    msg: object,
    state: OrchestratorState,
    queue: GroupQueue,
    get_executor: Callable[[str], Any],
    emit_chip_event: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    data = json.loads(msg.data.decode())  # type: ignore[attr-defined]

    # ---- 1. Parse + validate request ---------------------------------
    tenant_id = str(data["tenantId"])
    from_id = str(data["fromCoworkerId"])
    parent_conv_id = str(data["fromConversationId"])
    target_slug = str(data["target"])
    prompt = str(data["prompt"])
    ctx_mode = str(data["contextMode"])
    depth = int(data.get("depth", 0))
    user_id = data.get("userId")
    if user_id is not None:
        user_id = str(user_id)

    from_cs = state.coworkers.get(from_id)
    if from_cs is None:
        return _err("Calling coworker not found.")
    from_co = from_cs.config
    if from_co.tenant_id != tenant_id:
        # Cross-tenant: never resolve, never delegate.
        return _err("Tenant mismatch.")
    if from_co.permissions is None or not from_co.permissions.agent_delegate:
        return _err(f"{from_co.name} cannot delegate.")
    if depth >= MAX_DELEGATION_DEPTH:
        # Mutation guard: test #6 asserts the literal "max 1 hop" — if
        # someone flips ``MAX_DELEGATION_DEPTH`` from 1 to 2, the test
        # fails immediately. A bare ``isError`` check would silently
        # accept the broken value and let A→B→C ship.
        return _err(
            f"Delegation depth {depth} exceeds limit (max 1 hop)."
        )
    if ctx_mode not in ("sticky", "isolated"):
        return _err(f"contextMode must be 'sticky' or 'isolated'; got {ctx_mode!r}.")

    # ---- 2. Resolve target ------------------------------------------
    target_co = _resolve_target(
        state, from_co.tenant_id, target_slug, exclude_id=from_co.id,
    )
    if target_co is None:
        catalog = render_agent_catalog(
            state, from_co.tenant_id, exclude=from_co.id,
        )
        return _err(f"Agent '{target_slug}' not found.\n\n{catalog}")

    # ---- 3. Idempotent internal binding -----------------------------
    binding = await get_or_create_internal_binding(
        tenant_id=target_co.tenant_id, coworker_id=target_co.id,
    )

    # ---- 4. Find or create child conv (chat_id distinguishes mode) --
    sticky_chat_id = f"internal:{parent_conv_id}:{target_co.id}"
    if ctx_mode == "sticky":
        child_conv = await find_child_conversation(
            tenant_id=target_co.tenant_id,
            parent_conversation_id=parent_conv_id,
            target_coworker_id=target_co.id,
            channel_chat_id=sticky_chat_id,
        )
        if child_conv is None:
            child_conv = await create_child_conversation(
                tenant_id=target_co.tenant_id,
                parent_conversation_id=parent_conv_id,
                target_coworker_id=target_co.id,
                target_internal_binding_id=binding.id,
                user_id=user_id,
                mode="sticky",
            )
    else:
        child_conv = await create_child_conversation(
            tenant_id=target_co.tenant_id,
            parent_conversation_id=parent_conv_id,
            target_coworker_id=target_co.id,
            target_internal_binding_id=binding.id,
            user_id=user_id,
            mode="isolated",
        )

    # ---- 5. Resolve session_id (sticky only) ------------------------
    session_id: str | None = None
    if ctx_mode == "sticky":
        session_id = await get_session(
            child_conv.id, tenant_id=target_co.tenant_id,
        )

    # ---- 6. Insert audit row ----------------------------------------
    delegation_id = await insert_delegation(
        tenant_id=target_co.tenant_id,
        parent_conversation_id=parent_conv_id,
        child_conversation_id=child_conv.id,
        from_coworker_id=from_co.id,
        target_coworker_id=target_co.id,
        user_id=user_id,
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        context_mode=ctx_mode,
    )
    started = time.monotonic()

    # ---- 6b. Refuse early if queue is shutting down (contract (h)) --
    if getattr(queue, "_shutting_down", False):
        duration_ms = int((time.monotonic() - started) * 1000)
        await update_delegation_terminal(
            delegation_id,
            tenant_id=target_co.tenant_id,
            status="error",
            duration_ms=duration_ms,
            error_message="GroupQueue is shutting down; delegation refused.",
        )
        return _err("GroupQueue is shutting down; delegation refused.")

    # ---- 7. Resolve executor ----------------------------------------
    executor = get_executor(target_co.agent_backend)
    if executor is None:
        duration_ms = int((time.monotonic() - started) * 1000)
        msg_text = f"No executor for backend {target_co.agent_backend!r}."
        await update_delegation_terminal(
            delegation_id,
            tenant_id=target_co.tenant_id,
            status="error",
            duration_ms=duration_ms,
            error_message=msg_text,
        )
        return _err(msg_text)

    # ---- 8. Build AgentInput (factored for test #17a) ---------------
    agent_input = _build_agent_input(
        target_co=target_co,
        from_co=from_co,
        child_conv_chat_id=child_conv.channel_chat_id,
        child_conv_id=child_conv.id,
        prompt=prompt,
        user_id=user_id,
        session_id=session_id,
        depth=depth,
        parent_conv_id=parent_conv_id,
        delegation_id=delegation_id,
    )

    # ---- 8b. Frontdesk v1.5 child-chip emitter ----------------------
    # See plan §3 (Target Agent Event Stream Visualization). The chip
    # emitter is fire-and-forget: failures must NEVER block the audit
    # path or alter the merge precedence. Throttle bucket coalesces
    # bursty tool_use/running events at the source so the WebSocket
    # doesn't get hammered.
    chip_throttle = ChipThrottleBucket()

    async def _emit_chip(phase: str, payload: dict[str, Any]) -> None:
        if emit_chip_event is None:
            return
        try:
            await emit_chip_event(
                parent_conv_id=parent_conv_id,
                child_conv_id=child_conv.id,
                delegation_id=delegation_id,
                target_folder=target_co.folder,
                target_name=target_co.name,
                phase=phase,
                payload=payload,
            )
        except Exception:  # noqa: BLE001 — chip emit is best-effort
            logger.debug("chip emit raised; suppressed", phase=phase, exc_info=True)

    # Strong reference set for fire-and-forget chip emit tasks. Without
    # holding a ref the GC can collect the task mid-flight and the emit
    # silently drops; with it, we let the task complete and discard the
    # ref on done.
    chip_emit_tasks: set[asyncio.Task[None]] = set()

    def _schedule_chip(phase: str, payload: dict[str, Any]) -> None:
        # Fire-and-forget. The audit path must not block on chip emits.
        try:
            task = asyncio.create_task(_emit_chip(phase, payload))
        except RuntimeError:
            return
        chip_emit_tasks.add(task)
        task.add_done_callback(chip_emit_tasks.discard)

    async def _flush_and_close(final_status: str, duration_ms: int) -> None:
        # Flush any deferred throttled events first so their order is
        # preserved relative to close (UI would otherwise see a stale
        # "running" line linger after close).
        for phase, payload in chip_throttle.flush_all():
            await _emit_chip(phase, payload)
        await _emit_chip("close", {
            "final_status": final_status,
            "duration_ms": duration_ms,
        })

    # Open the chip immediately. Done before enqueue so the user sees
    # the sub-chip appear even while the target's container is still
    # cold-starting; the dev-only ``context_mode`` tag distinguishes
    # sticky vs isolated for debugging.
    _schedule_chip("open", {
        "initial_status": "queued",
        "context_mode": ctx_mode,
    })

    # ---- 9. Run + collect events ------------------------------------
    # Pi backend split: text-bearing event (is_final=False) + marker
    # (is_final=True, result=None). Claude backend emits a single event
    # with text + is_final=True. We track all three event kinds and
    # let ``_merge_pi_two_event_pattern`` decide the precedence: any
    # ``terminal_event`` (error / safety_blocked / stopped) ALWAYS
    # beats a prior success marker so a late safety_blocked from a
    # post-MODEL hook doesn't get masked by an earlier "success".
    text_event: AgentOutput | None = None
    final_marker: AgentOutput | None = None
    terminal_event: AgentOutput | None = None

    # Two futures — see contract (c).
    loop = asyncio.get_running_loop()
    started_future: asyncio.Future[None] = loop.create_future()
    result_future: asyncio.Future[None] = loop.create_future()
    queue_key = f"delegate:{child_conv.id}"

    async def _on_output(out: AgentOutput) -> None:
        nonlocal text_event, final_marker, terminal_event
        try:
            # v1.5: translate progress events into child-chip emits.
            # Done BEFORE the success/terminal branches so an out-of-spec
            # backend that mis-encodes a progress event (e.g. as
            # success+is_final=False with result=None and status="tool_use")
            # is still picked up here; the existing branches won't
            # double-count because PROGRESS_STATUSES is disjoint from
            # the terminal/success Literal set.
            if out.status in PROGRESS_STATUSES:
                _translate_progress_to_chip(out, chip_throttle, _schedule_chip)
                return
            # Pi text event (no marker yet, no terminal yet)
            if out.status == "success" and not out.is_final:
                if out.result:
                    text_event = out
                # Progress events (running / queued / tool_use)
                # are also success+is_final=False with result=None;
                # they harmlessly pass through here.
            elif out.status == "success" and out.is_final:
                final_marker = out
                # Claude single-event shape: ``is_final=True`` AND a
                # non-None ``result``. The Pi pattern's text event
                # never arrived because Claude emits one event total.
                # Record the same event as ``text_event`` so the merge
                # function picks up the result text. Skipped when
                # ``result`` is None (the pure Pi marker case) so the
                # merge's "marker without prior text" defensive
                # warning still fires for the genuine inverse drift.
                if out.result and text_event is None:
                    text_event = out
                # Container won't self-exit (contract (b)).
                queue.request_shutdown(queue_key)
                if not result_future.done():
                    result_future.set_result(None)
            elif out.status in ("error", "safety_blocked", "stopped"):
                terminal_event = out
                queue.request_shutdown(queue_key)
                if not result_future.done():
                    result_future.set_result(None)
        except Exception:
            # On-output failures must NOT propagate; they'd cancel the
            # executor task mid-stream and starve result_future. Log
            # and swallow.
            logger.exception("on_output handler raised; not propagating")

    def _on_process(container_name: str, job_id: str) -> None:
        # Bind the container/job to our queue_key so
        # ``queue.request_shutdown`` can target it.
        queue.register_process(
            queue_key, container_name,
            group_folder=target_co.folder, job_id=job_id,
        )

    async def _closure() -> None:
        # Signal "the closure started" — separate from result so the
        # OUTER_GUARD truly only catches "never started" (contract (c)).
        if not started_future.done():
            started_future.set_result(None)
        try:
            await asyncio.wait_for(
                executor.execute(agent_input, _on_process, _on_output),
                timeout=DEFAULT_BUSINESS_DEADLINE_S,
            )
            if not result_future.done():
                # Closure finished but ``_on_output`` never saw a
                # terminal event. Rare; happens if the backend exits
                # cleanly without emitting any AgentOutput. Treated as
                # a degenerate success with an empty result so the
                # frontdesk still gets something back.
                result_future.set_result(None)
        except TimeoutError as e:
            # Business deadline tripped: tell the container to exit
            # (contract (b)) and surface the timeout to the awaiter.
            queue.request_shutdown(queue_key)
            if not result_future.done():
                result_future.set_exception(e)
        except Exception as e:  # noqa: BLE001 — surface every closure failure
            if not result_future.done():
                result_future.set_exception(e)

    queue.enqueue_task(
        queue_key, f"delegate-{delegation_id}", _closure,
        tenant_id=target_co.tenant_id, coworker_id=target_co.id,
    )

    # ---- 10. Wait on started_future first ---------------------------
    # OUTER_GUARD only fences "closure never ran". If the queue is
    # genuinely stalled, ``started_future`` is never set and the
    # ``wait_for`` raises asyncio.TimeoutError after 30s. The audit
    # message MUST differ from the business-timeout one — test #22.
    try:
        await asyncio.wait_for(started_future, timeout=OUTER_GUARD_S)
    except TimeoutError:
        duration_ms = int((time.monotonic() - started) * 1000)
        await update_delegation_terminal(
            delegation_id,
            tenant_id=target_co.tenant_id,
            status="error",
            duration_ms=duration_ms,
            error_message="Delegation task never started (queue stalled).",
        )
        await _flush_and_close("error", duration_ms)
        return _err("Delegation task failed to start.")

    # ---- 11. Closure has started; wait for terminal -----------------
    # No outer timeout here — the business deadline lives inside the
    # closure and surfaces through ``result_future.set_exception``.
    try:
        await result_future
    except TimeoutError:
        # Business deadline tripped inside the closure.
        duration_ms = int((time.monotonic() - started) * 1000)
        msg_text = f"{target_co.name} took too long; aborted."
        await update_delegation_terminal(
            delegation_id,
            tenant_id=target_co.tenant_id,
            status="timeout",
            duration_ms=duration_ms,
            error_message=msg_text,
        )
        await _flush_and_close("timeout", duration_ms)
        return _timeout_response(
            target_co, delegation_id, child_conv.id, duration_ms,
        )
    except Exception as e:  # noqa: BLE001 — convert any closure failure into an audit-tracked error
        duration_ms = int((time.monotonic() - started) * 1000)
        await update_delegation_terminal(
            delegation_id,
            tenant_id=target_co.tenant_id,
            status="error",
            duration_ms=duration_ms,
            error_message=str(e),
        )
        await _flush_and_close("error", duration_ms)
        return _error_response(
            target_co, delegation_id, child_conv.id, duration_ms, str(e),
        )

    # ---- 12. Closure finished normally — merge + map ---------------
    final_out = _merge_pi_two_event_pattern(
        text_event, final_marker, terminal_event,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    response, audit_status = _map_output_to_response(
        final_out, target_co, delegation_id, child_conv.id, duration_ms,
    )

    # Sticky session persistence (contract (d)). The orchestrator's
    # ``_run_agent`` wrapper for set_session does NOT cover this path;
    # we MUST call set_session explicitly so the next sticky call
    # resumes the SDK session. DB failure here is non-fatal — the
    # delegation actually succeeded; we log and let the next sticky
    # call cold-start a fresh session.
    if (
        ctx_mode == "sticky"
        and final_out.status == "success"
        and final_out.new_session_id
    ):
        try:
            await set_session(
                child_conv.id,
                target_co.tenant_id,
                target_co.id,
                final_out.new_session_id,
            )
        except Exception as e:  # noqa: BLE001 — sticky session save is best-effort
            # Handbook §9 #10 — sticky persistence is best-effort. The
            # delegation succeeded; we still report success but warn
            # so ops can spot recurring DB hiccups. The next sticky
            # call cold-starts a fresh session.
            logger.warning(
                "sticky session save failed; next sticky call from "
                "this parent/target will start a fresh session",
                delegation_id=delegation_id,
                error=str(e),
            )

    await update_delegation_terminal(
        delegation_id,
        tenant_id=target_co.tenant_id,
        status=audit_status,
        duration_ms=duration_ms,
        error_message=(
            response.get("text") if audit_status != "success" else None
        ),
    )
    await _flush_and_close(audit_status, duration_ms)
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _translate_progress_to_chip(
    out: AgentOutput,
    bucket: ChipThrottleBucket,
    schedule: Callable[[str, dict[str, Any]], None],
) -> None:
    """Translate a target ``AgentOutput`` progress event into a chip emit.

    Frontdesk v1.5 §4 (UI behaviour): the parent web UI renders a
    single-line ephemeral sub-chip per active delegation. Tool-use
    events and ``running`` heartbeats are throttled to one emit per
    500ms per ``phase`` so the WS doesn't get hammered; the bucket's
    ``flush_all`` (called from ``_flush_and_close``) emits any
    last-window payload before close so a burst right before
    termination isn't lost.

    Phases:
      - "tool_use": payload {tool_name, tool_input}; UI may beautify
        ``mcp__server__tool`` into ``server > tool``.
      - "status":   payload {status: <PROGRESS_STATUSES value>}; UI
        renders a human-readable label (Queued / Starting / Thinking).
    """
    if out.status == "tool_use":
        meta = out.metadata or {}
        payload: dict[str, Any] = {
            "phase_kind": "tool_use",
            "tool_name": meta.get("tool"),
            "tool_input": meta.get("input"),
        }
        emit_now, prior = bucket.should_emit("tool_use", payload)
        if emit_now:
            if prior is not None:
                schedule("tool_use", prior)
            schedule("tool_use", payload)
        return
    # Generic progress (running / queued / container_starting). All
    # share a single "status" phase slot — last write wins, since the
    # UI only renders a single line.
    payload = {"phase_kind": "status", "status": out.status}
    emit_now, prior = bucket.should_emit("status", payload)
    if emit_now:
        if prior is not None:
            schedule("status", prior)
        schedule("status", payload)


def _resolve_target(
    state: OrchestratorState,
    tenant_id: str,
    target_slug: str,
    *,
    exclude_id: str,
) -> Coworker | None:
    """Find the delegation target — folder first, then UUID.

    Filters (handbook §6 Step 5.3): same tenant, ``status='active'``,
    not ``is_frontdesk`` (a specialist is any non-frontdesk coworker; the
    ``agent_role`` axis was removed upstream), not self.

    Two-key matching mitigates a real LLM failure mode: although the
    catalog renders ``(id: <folder>)`` and tells the model to pass the
    "agent id", the literal ``id:`` label sometimes nudges a model to
    pass the actual UUID instead. Folder-only matching would surface
    this as "Agent 'xxxx-xxxx' not found." with a catalog re-render
    that's no use (catalog still uses folders). UUID fallback resolves
    this with one extra dict lookup; folder and UUID don't collide.
    """
    target_cs = state.get_coworker_by_folder(tenant_id, target_slug)
    if target_cs is None:
        # Fallback: try as UUID directly.
        target_cs = state.coworkers.get(target_slug)
    if target_cs is None:
        return None
    c = target_cs.config
    if c.tenant_id != tenant_id:
        return None
    if c.is_frontdesk:
        return None
    if c.status != "active":
        return None
    if c.id == exclude_id:
        return None
    return c


def _merge_pi_two_event_pattern(
    text_event: AgentOutput | None,
    final_marker: AgentOutput | None,
    terminal_event: AgentOutput | None,
) -> AgentOutput:
    """Merge the three possible event shapes into a single AgentOutput.

    Precedence (contract (a)):
      1. ``terminal_event`` (error / safety_blocked / stopped) — a late
         terminal event from a post-MODEL safety hook MUST beat any
         earlier success marker. Test 13c pins this.
      2. ``final_marker`` + ``text_event`` — Pi pattern: text from event
         #1, ``new_session_id`` from event #2.
      3. ``final_marker`` only — degenerate. Log a defensive warning;
         empty result.
      4. ``text_event`` only — Claude single-event shape, OR a closure
         that ended via ``executor.execute`` returning before a marker
         arrived. ``new_session_id`` taken from the text event itself.
      5. nothing — empty success. Rare.
    """
    if terminal_event is not None:
        return terminal_event
    if text_event is not None and final_marker is not None:
        return AgentOutput(
            status="success",
            result=text_event.result,
            new_session_id=(
                final_marker.new_session_id or text_event.new_session_id
            ),
            is_final=True,
            metadata=text_event.metadata,
        )
    if final_marker is not None and text_event is None:
        logger.warning(
            "delegation merge: got marker without prior text event; "
            "returning empty success. Possible backend behaviour drift."
        )
        return AgentOutput(
            status="success",
            result=None,
            new_session_id=final_marker.new_session_id,
            is_final=True,
        )
    if text_event is not None:
        return AgentOutput(
            status="success",
            result=text_event.result,
            new_session_id=text_event.new_session_id,
            is_final=True,
            metadata=text_event.metadata,
        )
    return AgentOutput(
        status="success", result=None, new_session_id=None, is_final=True,
    )


def _build_agent_input(
    *,
    target_co: Coworker,
    from_co: Coworker,
    child_conv_chat_id: str,
    child_conv_id: str,
    prompt: str,
    user_id: str | None,
    session_id: str | None,
    depth: int,
    parent_conv_id: str,
    delegation_id: str,
) -> AgentInput:
    """Build the target's ``AgentInput`` from a delegation request.

    Factored out so test #17a can call it directly with synthetic
    inputs and pin the EXACT ``role_config`` shape — a loose membership
    check on the dict keys would let a regression slip a fifth field
    through (e.g. accidentally including ``from_permissions``) without
    surfacing in tests.

    ``permissions`` come from the TARGET's coworker, never the caller
    (handbook §8 #4). Otherwise frontdesk's bash perms would shadow
    the target's tighter sandbox.
    """
    assert target_co.permissions is not None  # always set by __post_init__
    return AgentInput(
        prompt=prompt,
        group_folder=target_co.folder,
        chat_jid=child_conv_chat_id,
        permissions=target_co.permissions.to_dict(),
        tenant_id=target_co.tenant_id,
        coworker_id=target_co.id,
        conversation_id=child_conv_id,
        user_id=user_id or "",
        session_id=session_id,
        is_scheduled_task=False,
        assistant_name=target_co.name,
        system_prompt=target_co.system_prompt,
        role_config={
            "is_delegated_call": True,
            "delegated_by": from_co.id,
            "delegation_depth": depth + 1,
            "parent_conversation_id": parent_conv_id,
            "delegation_id": delegation_id,
        },
    )


def _map_output_to_response(
    output: AgentOutput,
    target_co: Coworker,
    delegation_id: str,
    child_conv_id: str,
    duration_ms: int,
) -> tuple[dict[str, Any], str]:
    """Translate a target ``AgentOutput`` into wire payload + audit status.

    The audit status is what we write to ``delegations.status``; the
    response is what the calling frontdesk's ``delegate_to_agent`` tool
    sees. ``stopped`` collapses to audit ``error`` because the user-
    facing notion of "the target was interrupted" maps to the same
    "isError=true with a reason" surface as a hard error.
    """
    base_metadata: dict[str, Any] = {
        "targetCoworkerId": target_co.id,
        "targetFolder": target_co.folder,
        "childConversationId": child_conv_id,
        "newSessionId": output.new_session_id,
        "durationMs": duration_ms,
        "delegationId": delegation_id,
    }

    if output.status == "success":
        return (
            {
                "status": "success",
                "text": output.result or "",
                "metadata": {**base_metadata, "safetyStage": None},
                "isError": False,
            },
            "success",
        )
    if output.status == "safety_blocked":
        safety_stage = None
        if output.metadata and isinstance(output.metadata, dict):
            safety_stage = output.metadata.get("stage")
        return (
            {
                "status": "safety_blocked",
                "text": f"{target_co.name} declined: {output.result or ''}",
                "metadata": {**base_metadata, "safetyStage": safety_stage},
                "isError": True,
            },
            "safety_blocked",
        )
    if output.status == "stopped":
        return (
            {
                "status": "error",
                "text": f"{target_co.name} was interrupted.",
                "metadata": {**base_metadata, "safetyStage": None},
                "isError": True,
            },
            "error",
        )
    # output.status == "error" (or any unexpected status)
    err = output.error or output.result or "unknown error"
    return (
        {
            "status": "error",
            "text": f"{target_co.name} failed: {err}",
            "metadata": {**base_metadata, "safetyStage": None},
            "isError": True,
        },
        "error",
    )


def _err(text: str) -> dict[str, Any]:
    """Build an early-error wire response (no metadata).

    Used for validation failures (tenant mismatch, permission denied,
    target not found, etc.) and infrastructure failures (no executor,
    queue shutting down). The matching audit row has already been
    written (or is not applicable because we never inserted one).
    """
    return {"status": "error", "text": text, "isError": True, "metadata": None}


def _timeout_response(
    target_co: Coworker,
    delegation_id: str,
    child_conv_id: str,
    duration_ms: int,
) -> dict[str, Any]:
    """Wire response for the business-deadline timeout path."""
    return {
        "status": "timeout",
        "text": f"{target_co.name} took too long; aborted.",
        "metadata": {
            "targetCoworkerId": target_co.id,
            "targetFolder": target_co.folder,
            "childConversationId": child_conv_id,
            "newSessionId": None,
            "durationMs": duration_ms,
            "safetyStage": None,
            "delegationId": delegation_id,
        },
        "isError": True,
    }


def _error_response(
    target_co: Coworker,
    delegation_id: str,
    child_conv_id: str,
    duration_ms: int,
    error_text: str,
) -> dict[str, Any]:
    """Wire response for closure-raised errors (non-timeout)."""
    return {
        "status": "error",
        "text": f"{target_co.name} failed: {error_text}",
        "metadata": {
            "targetCoworkerId": target_co.id,
            "targetFolder": target_co.folder,
            "childConversationId": child_conv_id,
            "newSessionId": None,
            "durationMs": duration_ms,
            "safetyStage": None,
            "delegationId": delegation_id,
        },
        "isError": True,
    }
