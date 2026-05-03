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
from rolemesh.observability import (
    attach_parent_context,
    get_tracer,
    install_tracer,
    shutdown_tracer,
)

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
from .hooks.handlers import ApprovalHookHandler, TranscriptArchiveHandler
from .hooks.observability_handler import OtelHookHandler
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


async def drain_nats_input(sub: Any) -> list[str]:
    """Drain any pending input messages from an existing subscription."""
    messages: list[str] = []
    while True:
        try:
            msg = await asyncio.wait_for(sub.next_msg(timeout=0.1), timeout=0.1)
            data = json.loads(msg.data)
            await msg.ack()
            if data.get("type") == "message" and data.get("text"):
                messages.append(data["text"])
        except TimeoutError:
            break
    return messages


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------


# Truncation limit on the response text attribute so Langfuse / OTel
# backends don't choke on multi-KB attributes for long replies.
_CLAUDE_RESPONSE_PREVIEW_MAX = 500


def _emit_claude_message_span(event: ResultEvent) -> None:
    """Emit one ``claude.message`` span per Claude ResultEvent.

    Claude Agent SDK dispatches Anthropic API calls from a Node.js
    subprocess, so a Python-side instrumentor (OpenInference's
    AnthropicInstrumentor) cannot patch them. The only signal Python
    sees is the ResultEvent at the *end* of the call, carrying token
    usage and the final response text. We synthesise a span here:

    - Attributes follow OTel-GenAI semantic conventions
      (``gen_ai.*``) so Langfuse renders this as a Generation node
      and computes cost from its model registry automatically.
    - ``start_time`` is approximated as ``end - 1ms`` because the
      real provider call duration is unobservable from Python. The
      span is purely a marker — token counts and cost are accurate;
      latency is NOT.

    No-op when the tracer is in noop mode or the event has no usage.
    """
    if event.text is None and event.usage is None:
        return
    end_ns = _time_ns()
    # 1ms is just enough for Langfuse to render the span as a finite
    # bar rather than a zero-width point. The brief explicitly accepts
    # this approximation; see docs/observability/setup.md "Known
    # limitations" for the rationale.
    start_ns = end_ns - 1_000_000
    tracer = get_tracer("rolemesh.agent_runner")
    span = tracer.start_span("claude.message", start_time=start_ns)
    try:
        span.set_attribute("gen_ai.system", "anthropic")
        usage = event.usage
        if usage is not None:
            if usage.model_id:
                span.set_attribute("gen_ai.request.model", usage.model_id)
                span.set_attribute("gen_ai.response.model", usage.model_id)
            span.set_attribute("gen_ai.usage.input_tokens", int(usage.input_tokens))
            span.set_attribute("gen_ai.usage.output_tokens", int(usage.output_tokens))
            # Anthropic-specific cache attribution. OTel-GenAI doesn't
            # standardise these yet but Langfuse recognises them and
            # surfaces them in its Generation view.
            if usage.cache_read_tokens:
                span.set_attribute(
                    "gen_ai.usage.cache_read_input_tokens",
                    int(usage.cache_read_tokens),
                )
            if usage.cache_write_tokens:
                span.set_attribute(
                    "gen_ai.usage.cache_creation_input_tokens",
                    int(usage.cache_write_tokens),
                )
        if event.text:
            text = event.text
            if len(text) > _CLAUDE_RESPONSE_PREVIEW_MAX:
                text = text[:_CLAUDE_RESPONSE_PREVIEW_MAX] + "...(truncated)"
            span.set_attribute("gen_ai.response.text", text)
    except Exception:  # noqa: BLE001 — span emit must not break the turn
        log("emit_claude_message_span: attribute set failed (non-fatal)")
    span.end(end_time=end_ns)


def _time_ns() -> int:
    """Indirection so tests can monkey-patch the clock."""
    import time

    return time.time_ns()


def _create_backend(backend_name: str) -> Any:
    """Create the appropriate backend based on AGENT_BACKEND env var."""
    if backend_name == "pi":
        from .pi_backend import PiBackend
        return PiBackend()
    else:
        from .claude_backend import ClaudeBackend
        return ClaudeBackend()


def _install_pi_instrumentors() -> None:
    """Install OpenInference instrumentors for Pi backend's LLM SDKs.

    Must run BEFORE the Pi backend imports openai / google-genai /
    boto3 so the monkey-patches attach to fresh module references —
    instrumenting after import doesn't reach captured references in
    the backend code.

    Each instrumentor is independent. We swallow ImportError so an
    operator who installs the observability extra without a given
    LLM SDK doesn't see install failures; we log other exceptions so
    a real bug isn't silently lost.
    """
    instrumentors: list[tuple[str, str, str]] = [
        ("openai", "openinference.instrumentation.openai", "OpenAIInstrumentor"),
        ("bedrock", "openinference.instrumentation.bedrock", "BedrockInstrumentor"),
        (
            "google_genai",
            "openinference.instrumentation.google_genai",
            "GoogleGenAIInstrumentor",
        ),
    ]
    for label, module_path, class_name in instrumentors:
        try:
            module = __import__(module_path, fromlist=[class_name])
            cls = getattr(module, class_name)
            cls().instrument()
            log(f"OpenInference {label} instrumentor installed")
        except ImportError:
            # Optional package not installed — silently skip.
            pass
        except Exception as exc:  # noqa: BLE001 — instrumentation must not break startup
            log(f"OpenInference {label} instrumentor failed: {exc}")


# ---------------------------------------------------------------------------
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
        job_id=job_id,
        chat_jid=init.chat_jid,
        group_folder=init.group_folder,
        permissions=init.permissions,
        tenant_id=init.tenant_id,
        coworker_id=init.coworker_id,
        conversation_id=init.conversation_id,
        user_id=init.user_id,
        mcp_tool_reversibility=mcp_tool_reversibility,
    )

    # Observability bootstrap — must precede backend creation so:
    #   1. install_tracer registers the global TracerProvider before
    #      any span is emitted.
    #   2. attach_parent_context links subsequent spans under the
    #      orchestrator's agent.turn span (W3C carrier from init).
    #   3. Pi instrumentors monkey-patch openai/google-genai/boto3
    #      before backend code captures references to those modules.
    # Noop unless [observability] extra is installed AND
    # OTEL_EXPORTER_OTLP_ENDPOINT is set in the container env.
    install_tracer(
        "rolemesh-agent",
        **{
            "rolemesh.tenant_id": init.tenant_id,
            "rolemesh.coworker_id": init.coworker_id,
            "rolemesh.conversation_id": init.conversation_id,
            "rolemesh.coworker_name": init.assistant_name or "",
            "rolemesh.agent_backend": AGENT_BACKEND,
        },
    )
    attach_parent_context(init.trace_context)
    if AGENT_BACKEND == "pi":
        _install_pi_instrumentors()

    # Create and initialize backend
    backend = _create_backend(AGENT_BACKEND)

    # Track session ID from backend events
    session_id: str | None = init.session_id

    def _usage_meta(event: BackendEvent) -> dict[str, Any] | None:
        """Extract the wire-format ``usage`` payload from a backend event.

        Centralizes the metadata key choice so all four event branches
        below stay in lock-step: status="success" / "error" / "stopped"
        / "safety_blocked" all serialize usage under the same metadata
        key, and consumers don't have to special-case per status.
        Returns None when the event has no usage so legacy wire bytes
        stay byte-equal — see ContainerOutput.to_dict for the rest of
        the no-op invariant.
        """
        usage = getattr(event, "usage", None)
        if usage is None:
            return None
        return {"usage": usage.to_metadata()}

    async def on_event(event: BackendEvent) -> None:
        nonlocal session_id
        if isinstance(event, ResultEvent):
            if event.new_session_id:
                session_id = event.new_session_id
            metadata = _usage_meta(event)
            # Claude SDK's API call happens in a Node.js subprocess that
            # OpenInference cannot reach; emit a synthetic span here so
            # token usage / model id reach Langfuse anyway. Pi backend
            # is covered by the OpenInference instrumentors installed
            # in run_query_loop's preamble, so we skip the manual span
            # there to avoid double-counting.
            if AGENT_BACKEND == "claude":
                _emit_claude_message_span(event)
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="success",
                    result=event.text,
                    new_session_id=session_id,
                    is_final=event.is_final,
                    metadata=metadata,
                ),
            )
        elif isinstance(event, RunningEvent):
            await publish_output(
                js, job_id,
                ContainerOutput(status="running", result=None),
            )
        elif isinstance(event, ToolUseEvent):
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="tool_use",
                    result=None,
                    metadata={"tool": event.tool, "input": event.input_preview},
                ),
            )
        elif isinstance(event, StoppedEvent):
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="stopped",
                    result=None,
                    new_session_id=session_id,
                    metadata=_usage_meta(event),
                ),
            )
            # Approval cancel cascade. Best-effort publish — the approval
            # stream may not exist at all in deployments without the
            # approval module, and a failure here must not block the
            # stop lifecycle. See docs/backend-stop-contract.md §8.
            try:
                await js.publish(
                    f"approval.cancel_for_job.{job_id}", b""
                )
            except Exception as exc:  # noqa: BLE001 — cascade is best-effort
                log(f"approval cancel cascade publish failed: {exc}")
        elif isinstance(event, SessionInitEvent):
            session_id = event.session_id
            log(f"Session initialized: {session_id}")
        elif isinstance(event, CompactionEvent):
            log("Compaction event received")
        elif isinstance(event, SafetyBlockEvent):
            # Route through its own status so the orchestrator can skip
            # the "store as assistant message" DB write and render the
            # reason as a distinct UI bubble. ``result`` carries the
            # human-readable reason (recorded in logs / Agent output
            # telemetry) while metadata carries the structured fields
            # the orchestrator matches on.
            #
            # Deliberately does NOT forward ``session_id``. Claude SDK's
            # SessionInitEvent fires on SystemMessage(init) — before any
            # user message is processed — but the session file is only
            # persisted after a real turn completes. If we propagate the
            # init-time SID to the orchestrator, next turn's ``--resume``
            # hits "No conversation found" and the container exits 1,
            # triggering the scheduler's retry loop indefinitely. Pi
            # backend tracks session differently (session_file written
            # eagerly) so this concern is Claude-specific.
            block_metadata: dict[str, Any] = {"stage": event.stage}
            if event.rule_id is not None:
                block_metadata["rule_id"] = event.rule_id
            if event.usage is not None:
                block_metadata["usage"] = event.usage.to_metadata()
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="safety_blocked",
                    result=event.reason,
                    new_session_id=None,
                    metadata=block_metadata,
                ),
            )
        elif isinstance(event, ErrorEvent):
            log(f"Backend error: {event.error}")
            # Do NOT forward session_id on error. The typical ErrorEvent
            # source is Claude CLI failing to resume a stale session
            # ("No conversation found with session ID: ..."), in which
            # case init.session_id IS the dead id we're trying to get
            # rid of. Forwarding it causes orchestrator _wrapped to
            # re-persist the dead id via set_session, creating a death
            # loop: next retry reads the same dead id from DB, resume
            # fails again, error fires again, id re-persisted. Same
            # class of bug the safety_blocked handler above addresses.
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="error",
                    result=None,
                    new_session_id=None,
                    error=event.error,
                    metadata=_usage_meta(event),
                ),
            )

    # Build the unified hook registry. TranscriptArchiveHandler replaces
    # the Claude-specific in-line archive logic that used to live in
    # claude_backend._create_pre_compact_hook; it now runs against either
    # backend's PreCompact event via the shared HookRegistry.
    hook_registry = HookRegistry()
    hook_registry.register(TranscriptArchiveHandler(assistant_name=init.assistant_name))
    # OtelHookHandler is registered unconditionally — it no-ops cleanly
    # when get_tracer returns the noop fallback, so installing it has
    # no cost when the observability extra isn't enabled.
    hook_registry.register(OtelHookHandler())
    # Register ApprovalHookHandler only when policies are provided. An empty
    # or missing approval_policies list means the approval module is inactive
    # for this run, and we keep the hook chain untouched so behaviour stays
    # bit-identical to pre-approval builds.
    if init.approval_policies:
        hook_registry.register(
            ApprovalHookHandler(
                policies=init.approval_policies,
                tool_ctx=tool_ctx,
            )
        )

    # Register SafetyHookHandler only when rules are provided. An empty
    # or missing safety_rules list means the Safety Framework is
    # inactive for this run, preserving zero runtime cost for agents
    # that do not have rules configured — same convention as approval.
    # Guard logic lives in rolemesh.safety.loader so the registration
    # decision is unit-testable without a full container startup.
    from rolemesh.safety.loader import maybe_register_safety_handler

    maybe_register_safety_handler(
        hook_registry=hook_registry,
        safety_rules=init.safety_rules,
        tool_ctx=tool_ctx,
        slow_check_specs=init.slow_check_specs,
        nats_client=nc,
    )

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

    # Build initial prompt
    prompt = init.prompt
    if init.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    pending = await drain_nats_input(input_sub)
    if pending:
        log(f"Draining {len(pending)} pending NATS messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

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
    except Exception as exc:
        log(f"Failed to read initial input from NATS KV: {exc}")
        await publish_output(
            js, JOB_ID,
            ContainerOutput(status="error", result=None, error=f"Failed to read input from NATS KV: {exc}"),
        )
        await nc.close()
        sys.exit(1)

    try:
        try:
            await run_query_loop(init, nc, js, JOB_ID)
        except Exception as exc:
            error_message = str(exc)
            log(f"Agent error: {error_message}")
            await publish_output(
                js, JOB_ID,
                ContainerOutput(status="error", result=None, error=error_message),
            )
            await nc.close()
            sys.exit(1)
        await nc.close()
    finally:
        # Force-flush BatchSpanProcessor so the tail of the trace
        # isn't dropped on container exit. SystemExit propagates
        # through finally — the flush happens on both success and
        # error paths. Noop unless observability is enabled.
        shutdown_tracer()
