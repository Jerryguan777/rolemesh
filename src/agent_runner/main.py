"""
RoleMesh Agent Runner — backend-agnostic NATS bridge.

Runs inside a Docker container. Reads initial config from NATS KV,
selects a backend (Claude SDK or Pi) based on AGENT_BACKEND env var,
and translates backend events into NATS publishes.

Input protocol:
  NATS KV: Reads initial config from KV bucket "agent-init" key JOB_ID
  NATS JetStream: Follow-up messages via agent.{JOB_ID}.input
  NATS request-reply: Shutdown signal via agent.{JOB_ID}.shutdown

Output protocol:
  NATS JetStream: Results published to agent.{JOB_ID}.results
  NATS JetStream: Messages and tasks via agent.{JOB_ID}.messages / .tasks
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nats
from nats.js.api import DeliverPolicy

from rolemesh.ipc.protocol import AgentInitData

from .backend import (
    BackendEvent,
    CompactionEvent,
    ErrorEvent,
    ResultEvent,
    RunningEvent,
    SafetyBlockEvent,
    SessionInitEvent,
    StoppedEvent,
    ToolUseEvent,
)
from .hooks import HookRegistry
from .hooks.handlers import TranscriptArchiveHandler
from .tools.context import ToolContext

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.client import JetStreamContext

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
JOB_ID = os.environ.get("JOB_ID", "")
AGENT_BACKEND = os.environ.get("AGENT_BACKEND", "claude")


@dataclass
class ContainerOutput:
    status: str  # "success" | "error" | "running" | "tool_use" | "stopped" | "safety_blocked"
    result: str | None
    new_session_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    # is_final is only meaningful for status="success". When False, the outer
    # scheduler must NOT treat this as end-of-turn (another reply is still
    # coming in the same run_prompt batch). Default True preserves legacy
    # single-reply semantics for status values that don't participate in
    # batched replies (running/tool_use/error/stopped).
    #
    # status="safety_blocked" is its own terminal status: the framework
    # intercepted the turn (INPUT_PROMPT hook, PRE_TOOL_CALL hook, or
    # orchestrator-side MODEL_OUTPUT pipeline). ``result`` carries the
    # user-facing reason and ``metadata={"stage": ..., "rule_id": ...}``
    # carries the structured payload. Orchestrator _on_output routes
    # this to a dedicated WS frame without writing to the messages
    # table — blocks are already audited in safety_decisions.
    is_final: bool = True
    # Only meaningful for status="error". False means the failure is a
    # deterministic configuration error (pi.ai.types.NonRetryableConfigError):
    # the orchestrator should fail the message once and surface it to the
    # user instead of walking the retry/backoff ladder. Default True keeps
    # every unclassified error on the existing retry path (fail-open to
    # retry), and absence on the wire means True for older containers.
    retryable: bool = True
    # Run attribution (single-writer refactor): the ``runs`` row
    # this event belongs to — the run of the prompt the backend was serving
    # when it emitted the event. Seeded from ``AgentInitData.run_id``,
    # updated from the ``run_id`` on follow-up input payloads (see
    # ``_RunAttribution``). None when the turn has no run (scheduled task)
    # or the orchestrator predates the field.
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "result": self.result}
        if self.new_session_id is not None:
            d["newSessionId"] = self.new_session_id
        if self.error is not None:
            d["error"] = self.error
        if self.metadata is not None:
            d["metadata"] = self.metadata
        # Emit isFinal only when it carries non-default information, so legacy
        # consumers keep seeing the same JSON shape.
        if not self.is_final:
            d["isFinal"] = False
        # Same convention: only non-default (False) goes on the wire.
        if not self.retryable:
            d["retryable"] = False
        # Same convention: absent when there is nothing to attribute.
        if self.run_id is not None:
            d["runId"] = self.run_id
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


async def publish_output(js: JetStreamContext, job_id: str, output: ContainerOutput) -> None:
    """Publish a result to JetStream (Channel 2)."""
    await js.publish(
        f"agent.{job_id}.results",
        json.dumps(output.to_dict()).encode(),
    )


def is_retryable_error(exc: BaseException) -> bool:
    """Classify an exception that killed the agent turn.

    Whitelist-by-type: only ``pi.ai.types.NonRetryableConfigError`` (raised
    at deterministic local validation points — provider tool-name/schema
    checks, unresolvable model ids) is non-retryable. Everything else —
    including errors we can't classify because the ``pi`` package isn't
    importable in this runtime — stays retryable, so a transient fault can
    never be misclassified as permanent (fail-open to retry).
    """
    try:
        from pi.ai.types import NonRetryableConfigError
    except ImportError:  # runtime without the pi package
        return True
    return not isinstance(exc, NonRetryableConfigError)


async def drain_nats_input(sub: Any) -> list[tuple[str, str | None]]:
    """Drain any pending input messages from an existing subscription.

    Returns ``(text, run_id)`` pairs; ``run_id`` is None on payloads from
    an orchestrator that predates run attribution.
    """
    messages: list[tuple[str, str | None]] = []
    while True:
        try:
            msg = await asyncio.wait_for(sub.next_msg(timeout=0.1), timeout=0.1)
            data = json.loads(msg.data)
            await msg.ack()
            if data.get("type") == "message" and data.get("text"):
                rid = data.get("run_id")
                messages.append(
                    (data["text"], rid if isinstance(rid, str) and rid else None)
                )
        except TimeoutError:
            break
    return messages


class _RunAttribution:
    """Maps backend output events to the ``runs`` row they answer.

    The container serves prompts strictly in order: the initial prompt
    (possibly covering several drained messages — attributed to the LAST
    drained run, mirroring the orchestrator's ``active_run_id``
    convention), then queued follow-ups FIFO. Each per-prompt ResultEvent
    consumes one entry; every other event (progress, error, stop,
    safety-block) is attributed to the prompt currently being served
    without consuming it. ``last`` covers events that arrive after the
    queue drained (e.g. the batch-final marker).

    When a prompt dies mid-serving (error), the remaining queued runs are
    left un-terminated — parity with pre-refactor behavior, where the
    retry path re-reads those messages anyway.
    """

    def __init__(self) -> None:
        self._pending: list[str | None] = []
        self._last: str | None = None

    def enqueue(self, run_id: str | None) -> None:
        self._pending.append(run_id)

    def current(self) -> str | None:
        """Run of the prompt being served right now (non-consuming)."""
        return self._pending[0] if self._pending else self._last

    def consume(self) -> str | None:
        """Attribute a per-prompt result and advance to the next prompt."""
        rid = self._pending.pop(0) if self._pending else self._last
        self._last = rid
        return rid


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


def _create_backend(backend_name: str) -> Any:
    """Create the appropriate backend based on AGENT_BACKEND env var."""
    if backend_name == "pi":
        from .pi_backend import PiBackend
        return PiBackend()
    else:
        from .claude_backend import ClaudeBackend
        return ClaudeBackend()


# ---------------------------------------------------------------------------
def _usage_meta(event: BackendEvent) -> dict[str, Any] | None:
    """Extract the wire-format ``usage`` payload from a backend event.

    Centralizes the metadata key choice so all terminal event branches
    stay in lock-step: status="success" / "error" / "stopped" /
    "safety_blocked" all serialize usage under the same metadata key, and
    consumers don't have to special-case per status. Returns None when the
    event has no usage so legacy wire bytes stay byte-equal — see
    ContainerOutput.to_dict for the rest of the no-op invariant.
    """
    usage = getattr(event, "usage", None)
    if usage is None:
        return None
    return {"usage": usage.to_metadata()}


def event_to_output(
    event: BackendEvent, session_id: str | None
) -> tuple[ContainerOutput | None, str | None]:
    """Pure mapping from a backend event to ``(output_to_publish,
    updated_session_id)``.

    Single source of truth for the bridge's event translation. Side
    effects (the NATS approval-cancel cascade on stop, logging) stay in
    ``run_query_loop.on_event`` so this function can be exercised directly
    in tests without a NATS connection — no inline re-implementation in the
    test, which would just be a mirror that drifts.
    """
    if isinstance(event, ResultEvent):
        if event.new_session_id:
            session_id = event.new_session_id
        return (
            ContainerOutput(
                status="success",
                result=event.text,
                new_session_id=session_id,
                is_final=event.is_final,
                metadata=_usage_meta(event),
            ),
            session_id,
        )
    if isinstance(event, RunningEvent):
        return ContainerOutput(status="running", result=None), session_id
    if isinstance(event, ToolUseEvent):
        return (
            ContainerOutput(
                status="tool_use",
                result=None,
                metadata={"tool": event.tool, "input": event.input_preview},
            ),
            session_id,
        )
    if isinstance(event, StoppedEvent):
        return (
            ContainerOutput(
                status="stopped",
                result=None,
                new_session_id=session_id,
                metadata=_usage_meta(event),
            ),
            session_id,
        )
    if isinstance(event, SessionInitEvent):
        return None, event.session_id
    if isinstance(event, CompactionEvent):
        return None, session_id
    if isinstance(event, SafetyBlockEvent):
        block_metadata: dict[str, Any] = {"stage": event.stage}
        if event.rule_id is not None:
            block_metadata["rule_id"] = event.rule_id
        if event.usage is not None:
            block_metadata["usage"] = event.usage.to_metadata()
        # Deliberately new_session_id=None: Claude SDK fires SessionInit on
        # SystemMessage(init) before any turn persists the session file, so
        # forwarding the init-time SID makes next turn's --resume hit "No
        # conversation found" and the container exit 1, looping the scheduler.
        return (
            ContainerOutput(
                status="safety_blocked",
                result=event.reason,
                new_session_id=None,
                metadata=block_metadata,
            ),
            session_id,
        )
    if isinstance(event, ErrorEvent):
        # Deliberately new_session_id=None: the typical ErrorEvent is Claude
        # CLI failing to resume a stale session, so init.session_id IS the
        # dead id. Forwarding it makes the orchestrator re-persist the dead
        # id -> next retry resumes the same dead id -> death loop.
        return (
            ContainerOutput(
                status="error",
                result=None,
                new_session_id=None,
                error=event.error,
                metadata=_usage_meta(event),
            ),
            session_id,
        )
    return None, session_id


# NATS bridge — runs a query and translates events to NATS publishes
# ---------------------------------------------------------------------------


async def run_query_loop(
    init: AgentInitData,
    nc: Client,
    js: JetStreamContext,
    job_id: str,
) -> None:
    """Main query loop: start backend, run prompts, handle follow-ups."""

    # Build tool context
    # V2 P0.4: flatten per-MCP-server reversibility tables so the hook
    # handler can resolve ``get_tool_reversibility`` in O(1) without
    # reconstructing the mapping on each call.
    mcp_tool_reversibility: dict[str, dict[str, bool]] = {}
    for spec in init.mcp_servers or []:
        rev = getattr(spec, "tool_reversibility", None) or {}
        if rev:
            mcp_tool_reversibility[spec.name] = dict(rev)

    tool_ctx = ToolContext(
        js=js,
        nc=nc,
        job_id=job_id,
        chat_jid=init.chat_jid,
        group_folder=init.group_folder,
        permissions=init.permissions,
        tenant_id=init.tenant_id,
        coworker_id=init.coworker_id,
        conversation_id=init.conversation_id,
        user_id=init.user_id,
        is_scheduled_task=init.is_scheduled_task,
        mcp_tool_reversibility=mcp_tool_reversibility,
        # AgentInitData.role_config may be None on the wire; normalise
        # here so downstream tool code never has to None-check. ``dict(...)``
        # also makes a shallow copy — defensive against a tool mutating
        # init.role_config and bleeding back into IPC state.
        role_config=dict(init.role_config or {}),
    )

    # Create and initialize backend
    backend = _create_backend(AGENT_BACKEND)

    # Track session ID from backend events
    session_id: str | None = init.session_id

    # Maps each output event to the ``runs`` row of the prompt being served.
    # Seeded below (after the input drain) with the initial prompt's run;
    # follow-up intake points enqueue as messages arrive.
    attribution = _RunAttribution()

    async def on_event(event: BackendEvent) -> None:
        nonlocal session_id
        output, session_id = event_to_output(event, session_id)
        if isinstance(event, SessionInitEvent):
            log(f"Session initialized: {session_id}")
        elif isinstance(event, CompactionEvent):
            log("Compaction event received")
        elif isinstance(event, ErrorEvent):
            log(f"Backend error: {event.error}")
        if output is not None:
            # Per-prompt result: consume the queue entry (next event belongs
            # to the next prompt). Everything else — progress, error, stop,
            # safety block — describes the prompt still being served.
            if isinstance(event, ResultEvent):
                output.run_id = attribution.consume()
            else:
                output.run_id = attribution.current()
            await publish_output(js, job_id, output)

    # Build the unified hook registry. TranscriptArchiveHandler replaces
    # the Claude-specific in-line archive logic that used to live in
    # claude_backend._create_pre_compact_hook; it now runs against either
    # backend's PreCompact event via the shared HookRegistry.
    hook_registry = HookRegistry()
    hook_registry.register(TranscriptArchiveHandler(assistant_name=init.assistant_name))

    # HITL approval plumbing (docs/12-hitl-approval-architecture.md §6 / §11.4). Both the
    # business-policy approval hook and the safety-pipeline require_approval
    # bridge publish agent.{job_id}.approval_request and block on a decision
    # relayed over agent.{job_id}.approval_decision (subscribed below).
    # APPROVAL_TIMEOUT is the in-band fallback bound; the startup assertion in
    # core/config keeps it strictly below the container watchdog floor.
    from rolemesh.core.config import APPROVAL_TIMEOUT

    from .approval.awaiter import ApprovalAwaiter
    from .hooks.handlers import ApprovalHookHandler, policies_from_snapshot

    async def _publish_approval(subject: str, payload: dict[str, Any]) -> None:
        await js.publish(subject, json.dumps(payload).encode())

    # Safety->approval bridge awaiter: wired only when this run carries safety
    # rules, so a rule-free agent pays nothing. The awaiter owns the same
    # block-and-await machinery the business hook uses; the safety hook calls it
    # on a PRE_TOOL_CALL require_approval verdict.
    safety_awaiter: ApprovalAwaiter | None = None
    if init.safety_rules:
        safety_awaiter = ApprovalAwaiter(
            publish=_publish_approval,
            job_id=job_id,
            timeout_ms=APPROVAL_TIMEOUT,
        )

    # Register SafetyHookHandler only when rules are provided. An empty
    # or missing safety_rules list means the Safety Framework is
    # inactive for this run, preserving zero runtime cost for agents
    # that do not have rules configured.
    # Guard logic lives in rolemesh.safety.loader so the registration
    # decision is unit-testable without a full container startup.
    from rolemesh.safety.loader import maybe_register_safety_handler

    maybe_register_safety_handler(
        hook_registry=hook_registry,
        safety_rules=init.safety_rules,
        tool_ctx=tool_ctx,
        slow_check_specs=init.slow_check_specs,
        nats_client=nc,
        approval_awaiter=safety_awaiter,
    )

    # Business-policy approval hook. Registered only when this run carries a
    # non-empty policy snapshot — mirrors the safety handler's
    # zero-cost-when-inactive rule. Blocks a matched MCP tool call in place
    # until the orchestrator relays a decision.
    approval_handler: ApprovalHookHandler | None = None
    approval_policies = policies_from_snapshot(init.approval_policies)
    if approval_policies:
        approval_handler = ApprovalHookHandler(
            publish=_publish_approval,
            policies=approval_policies,
            job_id=job_id,
            tenant_id=init.tenant_id,
            coworker_id=init.coworker_id,
            conversation_id=init.conversation_id or None,
            user_id=init.user_id or None,
            timeout_ms=APPROVAL_TIMEOUT,
        )
        hook_registry.register(approval_handler)
        log(f"Approval hook active ({len(approval_policies)} policy snapshot)")

    backend.subscribe(on_event)
    await backend.start(
        init,
        tool_ctx,
        mcp_servers=init.mcp_servers,
        hooks=hook_registry,
    )

    # Subscribe once for the entire loop lifetime to avoid JetStream
    # redelivery of already-consumed messages when ephemeral consumers
    # are repeatedly created and destroyed.
    shutdown_received = asyncio.Event()

    async def handle_shutdown(msg: Any) -> None:
        # Core NATS request-reply: the orchestrator's request() awaits this
        # ack to know the container really received the shutdown (it's a
        # lifecycle event — caller wants synchronous confirmation).
        await msg.respond(b"ack")
        shutdown_received.set()

    async def handle_interrupt(msg: Any) -> None:
        """User clicked Stop. Abort the current turn but keep the container
        alive. Unlike handle_shutdown, this does NOT set shutdown_received, so
        the main loop continues waiting for the next user message after abort.

        Ack pattern differs from handle_shutdown by design: interrupt flows
        over JetStream (fire-and-forget publish from orchestrator — scheduler
        doesn't wait), so msg.ack() is a JS consumer ack, not a request-reply.
        Interrupt is inherently best-effort — cancelling takes time anyway,
        and the orchestrator learns completion via StoppedEvent, not ack.
        """
        await msg.ack()
        log("Interrupt signal received, aborting current turn")
        await backend.abort()

    shutdown_sub = await nc.subscribe(f"agent.{job_id}.shutdown", cb=handle_shutdown)
    # Interrupt is on JetStream (DeliverPolicy.NEW, ordered consumer): the
    # Stop button publishes fire-and-forget and the message is stored until
    # our consumer picks it up, even if the event loop is busy with LLM
    # streaming. Core NATS callback subscriptions were unreliable here —
    # server-side SUB could race with client registration and the request
    # would NoRespondersError.
    interrupt_sub = await js.subscribe(
        f"agent.{job_id}.interrupt",
        cb=handle_interrupt,
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )
    input_sub = await js.subscribe(f"agent.{job_id}.input")

    # Approval decisions are relayed by the orchestrator over JetStream. We ack
    # on receipt and route the payload to whichever awaiter owns the request_id:
    # the business-policy hook and the safety bridge each own one, and request
    # ids are disjoint, so a "try business, then safety" route is first-wins (an
    # unknown/stale id is a no-op on both). The container is awaiting a Future,
    # not blocking the loop, so this push callback fires while an approval is
    # pending.
    _approval_handler = approval_handler
    _safety_awaiter = safety_awaiter

    def _route_approval_decision(data: dict[str, Any]) -> bool:
        if _approval_handler is not None and _approval_handler.resolve_decision(data):
            return True
        return bool(
            _safety_awaiter is not None and _safety_awaiter.resolve_decision(data)
        )

    approval_decision_sub = None
    if approval_handler is not None or safety_awaiter is not None:

        async def handle_approval_decision(msg: Any) -> None:
            await msg.ack()
            try:
                data = json.loads(msg.data)
            except (ValueError, TypeError) as exc:
                log(f"Malformed approval_decision dropped: {exc}")
                return
            if isinstance(data, dict) and not _route_approval_decision(data):
                log(
                    "approval_decision for unknown/stale request dropped: "
                    f"{data.get('request_id')}"
                )

        approval_decision_sub = await js.subscribe(
            f"agent.{job_id}.approval_decision",
            cb=handle_approval_decision,
            ordered_consumer=True,
            deliver_policy=DeliverPolicy.NEW,
        )

    # Build initial prompt
    prompt = init.prompt
    if init.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    # Run attribution: the merged initial prompt (init + drained messages)
    # is served as ONE prompt, answered by ONE ResultEvent, so it gets ONE
    # queue entry. When drained messages carry run ids, the last non-None
    # one wins — same "last message's run" convention the orchestrator uses
    # for active_run_id when it batches messages into a single prompt.
    initial_run_id = init.run_id
    pending = await drain_nats_input(input_sub)
    if pending:
        log(f"Draining {len(pending)} pending NATS messages into initial prompt")
        prompt += "\n" + "\n".join(text for text, _ in pending)
        for _, drained_run_id in pending:
            if drained_run_id is not None:
                initial_run_id = drained_run_id
    attribution.enqueue(initial_run_id)

    # Main query loop
    try:
        while True:
            log(f"Starting query (session: {session_id or 'new'})...")

            shutdown_during_query = False
            ipc_polling = True

            async def poll_nats_during_query() -> None:
                nonlocal ipc_polling, shutdown_during_query
                while ipc_polling:
                    if shutdown_received.is_set():
                        log("Shutdown signal detected during query")
                        shutdown_during_query = True
                        await backend.abort()
                        ipc_polling = False
                        return
                    try:
                        msg = await asyncio.wait_for(input_sub.next_msg(timeout=0.5), timeout=0.5)
                        data = json.loads(msg.data)
                        await msg.ack()
                        if data.get("type") == "message" and data.get("text"):
                            text = data["text"]
                            rid = data.get("run_id")
                            attribution.enqueue(
                                rid if isinstance(rid, str) and rid else None
                            )
                            log(f"Follow-up message received ({len(text)} chars)")
                            await backend.handle_follow_up(text)
                    except TimeoutError:
                        pass

            poll_task = asyncio.ensure_future(poll_nats_during_query())

            try:
                await backend.run_prompt(prompt)
            finally:
                ipc_polling = False
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task

            if shutdown_during_query:
                log("Shutdown signal consumed during query, exiting")
                break

            # Batch-final marker — the anchor of the is_final contract. Every
            # per-prompt ResultEvent emitted by the backend is is_final=False;
            # this publish is what releases host-side idle gating (notify_idle)
            # once the whole run_prompt call (initial + any queued follow-ups)
            # has settled. Keep is_final=True explicit, not relying on the
            # dataclass default, so the semantics don't silently regress if
            # the default changes.
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="success",
                    result=None,
                    new_session_id=session_id,
                    is_final=True,
                    # The batch has settled: attribute the marker to the last
                    # prompt served (attribution.current() falls back to
                    # ``last`` once the queue is empty). This is the event
                    # the orchestrator terminal-writes the run from.
                    run_id=attribution.current(),
                ),
            )

            log("Query ended, waiting for next NATS message...")

            # Wait for next input or shutdown signal using the shared subscriptions.
            next_message: str | None = None
            while True:
                if shutdown_received.is_set():
                    break
                try:
                    msg = await asyncio.wait_for(input_sub.next_msg(timeout=0.5), timeout=0.5)
                    data = json.loads(msg.data)
                    await msg.ack()
                    if data.get("type") == "message" and data.get("text"):
                        next_message = data["text"]
                        rid = data.get("run_id")
                        attribution.enqueue(
                            rid if isinstance(rid, str) and rid else None
                        )
                        break
                except TimeoutError:
                    pass

            if next_message is None:
                log("Shutdown signal received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message
    finally:
        await input_sub.unsubscribe()
        await shutdown_sub.unsubscribe()
        await interrupt_sub.unsubscribe()
        if approval_decision_sub is not None:
            await approval_decision_sub.unsubscribe()
        await backend.shutdown()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    if not JOB_ID:
        log("JOB_ID environment variable not set")
        sys.exit(1)

    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    log(f"Connected to NATS at {NATS_URL}, backend={AGENT_BACKEND}")

    # Read initial input from KV
    try:
        kv = await js.key_value("agent-init")
        entry = await kv.get(JOB_ID)
        init = AgentInitData.deserialize(entry.value)
        log(f"Received input for group: {init.group_folder}")
    except Exception as exc:  # noqa: BLE001
        log(f"Failed to read initial input from NATS KV: {exc}")
        await publish_output(
            js, JOB_ID,
            ContainerOutput(status="error", result=None, error=f"Failed to read input from NATS KV: {exc}"),
        )
        await nc.close()
        sys.exit(1)

    try:
        await run_query_loop(init, nc, js, JOB_ID)
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)
        retryable = is_retryable_error(exc)
        log(f"Agent error (retryable={retryable}): {error_message}")
        await publish_output(
            js, JOB_ID,
            ContainerOutput(
                status="error",
                result=None,
                error=error_message,
                retryable=retryable,
            ),
        )
        await nc.close()
        sys.exit(1)

    await nc.close()
