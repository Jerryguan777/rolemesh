"""WebNatsGateway — ChannelGateway implementation for the browser-based WebUI.

Communicates with a separate FastAPI process exclusively via NATS subjects
under the ``web-ipc`` JetStream stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING

from nats.js.api import DeliverPolicy

from rolemesh.core.logger import get_logger
from rolemesh.ipc.web_protocol import (
    WebInboundMessage,
    WebOutboundMessage,
    WebStreamChunk,
    WebTypingMessage,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nats.js.client import JetStreamContext

    from rolemesh.channels.gateway import MessageCallback
    from rolemesh.core.types import ChannelBinding
    from rolemesh.ipc.nats_transport import NatsTransport

logger = get_logger()


class WebNatsGateway:
    """ChannelGateway that bridges browser clients via NATS.

    The gateway subscribes to ``web.inbound.*`` to receive user messages
    forwarded by the FastAPI web server, and publishes streaming chunks,
    typing indicators, and complete replies back.
    """

    def __init__(self, on_message: MessageCallback, transport: NatsTransport) -> None:
        self._on_message = on_message
        self._transport = transport
        self._bindings: dict[str, ChannelBinding] = {}
        self._sub_task: asyncio.Task[None] | None = None
        self._stop_sub_task: asyncio.Task[None] | None = None
        self._approval_sub_task: asyncio.Task[None] | None = None
        self._on_stop: Callable[[str, str], Awaitable[None]] | None = None
        self._on_approval_decision: (
            Callable[[str, str, dict[str, object]], Awaitable[None]] | None
        ) = None

    def set_on_stop(self, fn: Callable[[str, str], Awaitable[None]]) -> None:
        """Register callback for browser-initiated Stop signals.

        fn(binding_id, chat_id) — called when a user clicks Stop in the
        WebUI. binding_id and chat_id are always authenticated values from
        the subject, never from client-controlled payload.
        """
        self._on_stop = fn

    def set_on_approval_decision(
        self, fn: Callable[[str, str, dict[str, object]], Awaitable[None]]
    ) -> None:
        """Register the HITL approval-decision callback from the WebUI.

        ``fn(binding_id, chat_id, body)`` — ``binding_id``/``chat_id`` come
        from the authenticated subject; ``body`` carries the WebUI-stamped
        ``decided_by``/``tenant_id``/``conversation_id`` (from the verified WS
        ticket) plus ``request_id``/``decision``. The orchestrator re-derives a
        tenant/conversation guard before relaying (IDOR, §10 S4).
        """
        self._on_approval_decision = fn

    # -- ChannelGateway protocol ------------------------------------------------

    @property
    def channel_type(self) -> str:
        return "web"

    async def add_binding(self, binding: ChannelBinding) -> None:
        self._bindings[binding.id] = binding

    async def remove_binding(self, binding_id: str) -> None:
        self._bindings.pop(binding_id, None)

    async def _refresh_binding(self, binding_id: str) -> bool:
        """Look the binding up in the DB and register if it exists.

        Returns ``True`` when the binding now lives in ``self._bindings``.
        Centralised here so the listener loop only owns the "ack on
        miss" decision. Uses the admin pool because the orchestrator
        runs without a tenant context (the binding row is what tells
        us which tenant it belongs to in the first place).
        """
        # Local import to avoid an import-cycle with the DB layer at
        # module load (rolemesh.db pulls in lots of typing-only code).
        from rolemesh.db import get_channel_binding_by_id_admin

        try:
            row = await get_channel_binding_by_id_admin(binding_id)
        except Exception:
            logger.exception("web_binding refresh failed", binding_id=binding_id)
            return False
        if row is None or row.channel_type != "web":
            return False
        self._bindings[row.id] = row
        logger.info("web_binding hot-loaded", binding_id=binding_id, tenant_id=row.tenant_id)
        return True

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        """Publish a complete agent reply to ``web.outbound.{binding_id}.{chat_id}``."""
        msg = WebOutboundMessage(text=text)
        await self._transport.js.publish(
            f"web.outbound.{binding_id}.{chat_id}",
            msg.to_bytes(),
        )

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        """Publish typing indicator to ``web.typing.{binding_id}.{chat_id}``."""
        msg = WebTypingMessage(is_typing=is_typing)
        await self._transport.js.publish(
            f"web.typing.{binding_id}.{chat_id}",
            msg.to_bytes(),
        )

    async def send_approval_event(
        self, binding_id: str, chat_id: str, payload: dict[str, object]
    ) -> None:
        """Publish a HITL approval card / resolution to ``web.approval.*``.

        The payload is a card-lifecycle event (``approval.requested`` /
        ``approval.resolved``); the WS handler whitelists fields into an
        ``event.approval.*`` browser frame.
        """
        await self._transport.js.publish(
            f"web.approval.{binding_id}.{chat_id}",
            json.dumps(payload).encode(),
        )

    async def shutdown(self) -> None:
        for task in (
            self._sub_task,
            self._stop_sub_task,
            self._approval_sub_task,
        ):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._sub_task = None
        self._stop_sub_task = None
        self._approval_sub_task = None

    # -- Web-specific streaming methods ----------------------------------------

    async def send_stream_chunk(self, binding_id: str, chat_id: str, content: str) -> None:
        """Publish a text chunk to ``web.stream.{binding_id}.{chat_id}``."""
        chunk = WebStreamChunk(type="text", content=content)
        await self._transport.js.publish(
            f"web.stream.{binding_id}.{chat_id}",
            chunk.to_bytes(),
        )

    async def send_stream_done(self, binding_id: str, chat_id: str) -> None:
        """Publish end-of-stream marker to ``web.stream.{binding_id}.{chat_id}``."""
        chunk = WebStreamChunk(type="done")
        await self._transport.js.publish(
            f"web.stream.{binding_id}.{chat_id}",
            chunk.to_bytes(),
        )

    async def send_status(
        self, binding_id: str, chat_id: str, payload: dict[str, object]
    ) -> None:
        """Publish a progress-status payload on the same stream subject.

        Status chunks piggyback on ``web.stream.*`` so they remain ordered
        relative to text/done. ws.py branches on chunk.type to separate them.
        """
        chunk = WebStreamChunk(type="status", content=json.dumps(payload))
        await self._transport.js.publish(
            f"web.stream.{binding_id}.{chat_id}",
            chunk.to_bytes(),
        )

    async def send_run_completed(
        self, binding_id: str, chat_id: str, *, run_id: str
    ) -> None:
        """Publish the run-terminal success marker (single-writer contract).

        Emitted by the orchestrator exactly once per run, AFTER the
        terminal DB write, so the WS projection (``event.run.completed``)
        can never contradict ``GET /api/v1/runs/{id}``. Rides the same
        ``web.stream.*`` subject as text/done so it stays ordered after
        the tokens it certifies.
        """
        chunk = WebStreamChunk(
            type="run_completed", content=json.dumps({"run_id": run_id})
        )
        await self._transport.js.publish(
            f"web.stream.{binding_id}.{chat_id}",
            chunk.to_bytes(),
        )

    async def send_run_error(
        self,
        binding_id: str,
        chat_id: str,
        *,
        run_id: str,
        error: dict[str, object],
    ) -> None:
        """Publish the run-terminal failure marker (single-writer contract).

        ``error`` mirrors the runs row's error JSONB ({code, message, ...}).
        Same ordering and once-per-run contract as ``send_run_completed``.
        """
        chunk = WebStreamChunk(
            type="run_error",
            content=json.dumps({"run_id": run_id, "error": error}),
        )
        await self._transport.js.publish(
            f"web.stream.{binding_id}.{chat_id}",
            chunk.to_bytes(),
        )

    async def send_safety_block(
        self,
        binding_id: str,
        chat_id: str,
        *,
        reason: str,
        stage: str,
        rule_id: str | None = None,
    ) -> None:
        """Publish a safety-block notification on the stream subject.

        Shares ``web.stream.*`` with text / done / status so the WS
        handler receives it in order relative to those. ws.py forwards
        it to the client as a distinct ``{"type":"safety_blocked"}``
        frame that the frontend renders as its own bubble kind.
        """
        payload: dict[str, object] = {"reason": reason, "stage": stage}
        if rule_id is not None:
            payload["rule_id"] = rule_id
        chunk = WebStreamChunk(type="safety_blocked", content=json.dumps(payload))
        await self._transport.js.publish(
            f"web.stream.{binding_id}.{chat_id}",
            chunk.to_bytes(),
        )

    # -- Lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to ``web.inbound.*`` and begin dispatching messages."""
        js: JetStreamContext = self._transport.js

        # Clean up stale durable consumer on startup
        with contextlib.suppress(Exception):
            await js.delete_consumer("web-ipc", "orch-web-inbound")
        with contextlib.suppress(Exception):
            await js.purge_stream("web-ipc")

        sub = await js.subscribe("web.inbound.*", durable="orch-web-inbound")

        async def _listener() -> None:
            async for msg in sub.messages:
                try:
                    inbound = WebInboundMessage.from_bytes(msg.data)
                    # Extract binding_id from subject: web.inbound.{binding_id}
                    parts = msg.subject.split(".")
                    binding_id = parts[2] if len(parts) >= 3 else ""

                    if binding_id not in self._bindings:
                        # Hot-reload: a binding row that didn't exist
                        # at orchestrator startup is the normal case
                        # the first time a v1 user opens the chat for
                        # a new coworker (the webui creates the row
                        # at conversation-create time). Look it up
                        # from DB and register on the fly instead of
                        # dropping the message. If the row truly
                        # doesn't exist (forged subject / cleanup
                        # race) we log + ack so the consumer drains.
                        registered = await self._refresh_binding(binding_id)
                        if not registered:
                            logger.warning("Unknown web binding_id", binding_id=binding_id)
                            await msg.ack()
                            continue

                    await self._on_message(
                        binding_id,
                        inbound.chat_id,
                        inbound.sender_id,
                        inbound.sender_name,
                        inbound.text,
                        inbound.timestamp,
                        inbound.msg_id,
                    )
                    await msg.ack()
                except Exception:
                    logger.exception("Error processing web inbound message")
                    with contextlib.suppress(Exception):
                        await msg.ack()

        self._sub_task = asyncio.create_task(_listener())

        # Subscribe to user-initiated Stop signals from FastAPI.
        # Subject: web.stop.{binding_id}.{chat_id}
        # Body is ignored — the authenticated identifiers are in the subject,
        # never in the payload (prevents IDOR from a compromised browser).
        with contextlib.suppress(Exception):
            await js.delete_consumer("web-ipc", "orch-web-stop")
        # deliver_policy=NEW so a restarted orchestrator doesn't replay old
        # stop signals whose conversations have already completed.
        stop_sub = await js.subscribe(
            "web.stop.*.*",
            durable="orch-web-stop",
            deliver_policy=DeliverPolicy.NEW,
        )

        async def _stop_listener() -> None:
            async for msg in stop_sub.messages:
                # One info-level log per stop is useful for ops diagnosing
                # "I clicked Stop but nothing happened" — keep, don't spam
                # additional logs in the downstream handlers.
                logger.info("Web stop received", subject=msg.subject)
                try:
                    parts = msg.subject.split(".")
                    # web.stop.{binding_id}.{chat_id} — 4 parts
                    if len(parts) >= 4 and self._on_stop is not None:
                        binding_id = parts[2]
                        chat_id = parts[3]
                        await self._on_stop(binding_id, chat_id)
                    await msg.ack()
                except Exception:
                    logger.exception("Error processing web stop")
                    with contextlib.suppress(Exception):
                        await msg.ack()

        self._stop_sub_task = asyncio.create_task(_stop_listener())

        # HITL approval decisions from the WebUI (§10 S4). Subject
        # web.approval_decision.{binding_id}.{chat_id}; the binding/chat in the
        # subject are authenticated, and the body carries the ticket-stamped
        # decided_by/tenant_id/conversation_id the WebUI verified. NEW so a
        # restart doesn't replay a decision whose request already resolved.
        with contextlib.suppress(Exception):
            await js.delete_consumer("web-ipc", "orch-web-approval-decision")
        approval_sub = await js.subscribe(
            "web.approval_decision.*.*",
            durable="orch-web-approval-decision",
            deliver_policy=DeliverPolicy.NEW,
        )

        async def _approval_listener() -> None:
            async for msg in approval_sub.messages:
                try:
                    parts = msg.subject.split(".")
                    if len(parts) >= 4 and self._on_approval_decision is not None:
                        binding_id = parts[2]
                        chat_id = parts[3]
                        body = json.loads(msg.data)
                        if isinstance(body, dict):
                            await self._on_approval_decision(
                                binding_id, chat_id, body
                            )
                    await msg.ack()
                except Exception:
                    logger.exception("Error processing web approval decision")
                    with contextlib.suppress(Exception):
                        await msg.ack()

        self._approval_sub_task = asyncio.create_task(_approval_listener())
        logger.info("WebNatsGateway started")
