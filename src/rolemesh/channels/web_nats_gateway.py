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
        self._on_stop: Callable[[str, str], Awaitable[None]] | None = None

    def set_on_stop(self, fn: Callable[[str, str], Awaitable[None]]) -> None:
        """Register callback for browser-initiated Stop signals.

        fn(binding_id, chat_id) — called when a user clicks Stop in the
        WebUI. binding_id and chat_id are always authenticated values from
        the subject, never from client-controlled payload.
        """
        self._on_stop = fn

    # -- ChannelGateway protocol ------------------------------------------------

    @property
    def channel_type(self) -> str:
        return "web"

    async def add_binding(self, binding: ChannelBinding) -> None:
        self._bindings[binding.id] = binding

    async def remove_binding(self, binding_id: str) -> None:
        self._bindings.pop(binding_id, None)

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

    async def shutdown(self) -> None:
        for task in (self._sub_task, self._stop_sub_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._sub_task = None
        self._stop_sub_task = None

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
                        False,  # web chat is never a group
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
        logger.info("WebNatsGateway started")
