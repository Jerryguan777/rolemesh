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
    status: str  # "success" | "error" | "running" | "tool_use" | "stopped"
    result: str | None
    new_session_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    # is_final is only meaningful for status="success". When False, the outer
    # scheduler must NOT treat this as end-of-turn (another reply is still
    # coming in the same run_prompt batch). Default True preserves legacy
    # single-reply semantics for status values that don't participate in
    # batched replies (running/tool_use/error/stopped).
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


def _create_backend(backend_name: str) -> Any:
    """Create the appropriate backend based on AGENT_BACKEND env var."""
    if backend_name == "pi":
        from .pi_backend import PiBackend
        return PiBackend()
    else:
        from .claude_backend import ClaudeBackend
        return ClaudeBackend()


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
    tool_ctx = ToolContext(
        js=js,
        job_id=job_id,
        chat_jid=init.chat_jid,
        group_folder=init.group_folder,
        permissions=init.permissions,
        tenant_id=init.tenant_id,
        coworker_id=init.coworker_id,
        conversation_id=init.conversation_id,
    )

    # Create and initialize backend
    backend = _create_backend(AGENT_BACKEND)

    # Track session ID from backend events
    session_id: str | None = init.session_id

    async def on_event(event: BackendEvent) -> None:
        nonlocal session_id
        if isinstance(event, ResultEvent):
            if event.new_session_id:
                session_id = event.new_session_id
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="success",
                    result=event.text,
                    new_session_id=session_id,
                    is_final=event.is_final,
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
                ContainerOutput(status="stopped", result=None, new_session_id=session_id),
            )
        elif isinstance(event, SessionInitEvent):
            session_id = event.session_id
            log(f"Session initialized: {session_id}")
        elif isinstance(event, CompactionEvent):
            log("Compaction event received")
        elif isinstance(event, ErrorEvent):
            log(f"Backend error: {event.error}")
            await publish_output(
                js, job_id,
                ContainerOutput(
                    status="error",
                    result=None,
                    new_session_id=session_id,
                    error=event.error,
                ),
            )

    # Build the unified hook registry. TranscriptArchiveHandler replaces
    # the Claude-specific in-line archive logic that used to live in
    # claude_backend._create_pre_compact_hook; it now runs against either
    # backend's PreCompact event via the shared HookRegistry.
    hook_registry = HookRegistry()
    hook_registry.register(TranscriptArchiveHandler(assistant_name=init.assistant_name))

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
