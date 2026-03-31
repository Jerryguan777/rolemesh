"""WebSocket handler — bridges browser clients to NATS."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from rolemesh.ipc.web_protocol import WebInboundMessage
from webui.auth import validate_token

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

# binding_id -> { chat_id -> WebSocket }
connections: dict[str, dict[str, WebSocket]] = {}

_js: JetStreamContext | None = None


def set_jetstream(js: JetStreamContext) -> None:
    """Set the shared JetStream context (called once on startup)."""
    global _js
    _js = js


async def handle_ws(ws: WebSocket, binding_id: str, token: str) -> None:
    """Handle a single WebSocket connection lifecycle."""
    if not validate_token(binding_id, token):
        await ws.close(code=1008, reason="Invalid token")
        return

    await ws.accept()

    assert _js is not None
    chat_id = str(uuid.uuid4())
    sender_id = f"web-user-{chat_id[:8]}"

    # Send session info
    await ws.send_json({"type": "session", "chatId": chat_id, "bindingId": binding_id})

    # Register connection
    connections.setdefault(binding_id, {})[chat_id] = ws

    # Subscribe to NATS subjects for this connection
    stream_sub = await _js.subscribe(
        f"web.stream.{binding_id}.{chat_id}",
        ordered_consumer=True,
    )
    typing_sub = await _js.subscribe(
        f"web.typing.{binding_id}.{chat_id}",
        ordered_consumer=True,
    )
    outbound_sub = await _js.subscribe(
        f"web.outbound.{binding_id}.{chat_id}",
        ordered_consumer=True,
    )

    async def _forward_stream() -> None:
        async for msg in stream_sub.messages:
            try:
                data = json.loads(msg.data)
                if data.get("type") == "text":
                    await ws.send_json({"type": "text", "content": data["content"]})
                elif data.get("type") == "done":
                    await ws.send_json({"type": "done"})
                await msg.ack()
            except (WebSocketDisconnect, RuntimeError):
                return
            except (OSError, ValueError, TypeError, KeyError):
                with contextlib.suppress(OSError, RuntimeError):
                    await msg.ack()

    async def _forward_typing() -> None:
        async for msg in typing_sub.messages:
            try:
                data = json.loads(msg.data)
                if data.get("is_typing"):
                    await ws.send_json({"type": "thinking"})
                await msg.ack()
            except (WebSocketDisconnect, RuntimeError):
                return
            except (OSError, ValueError, TypeError, KeyError):
                with contextlib.suppress(OSError, RuntimeError):
                    await msg.ack()

    async def _forward_outbound() -> None:
        async for msg in outbound_sub.messages:
            try:
                data = json.loads(msg.data)
                await ws.send_json({"type": "text", "content": data["text"]})
                await ws.send_json({"type": "done"})
                await msg.ack()
            except (WebSocketDisconnect, RuntimeError):
                return
            except (OSError, ValueError, TypeError, KeyError):
                with contextlib.suppress(OSError, RuntimeError):
                    await msg.ack()

    tasks = [
        asyncio.create_task(_forward_stream()),
        asyncio.create_task(_forward_typing()),
        asyncio.create_task(_forward_outbound()),
    ]

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            if data.get("type") == "message" and data.get("content"):
                inbound = WebInboundMessage(
                    chat_id=chat_id,
                    sender_id=sender_id,
                    sender_name="Web User",
                    text=data["content"],
                    timestamp=datetime.now(UTC).isoformat(),
                    msg_id=str(uuid.uuid4()),
                )
                await _js.publish(
                    f"web.inbound.{binding_id}",
                    inbound.to_bytes(),
                )
    except WebSocketDisconnect:
        pass
    except (OSError, ValueError, TypeError, RuntimeError):
        with contextlib.suppress(OSError, RuntimeError):
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_json({"type": "error", "message": "Internal server error"})
    finally:
        # Cleanup
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

        with contextlib.suppress(Exception):
            await stream_sub.unsubscribe()
        with contextlib.suppress(Exception):
            await typing_sub.unsubscribe()
        with contextlib.suppress(Exception):
            await outbound_sub.unsubscribe()

        binding_conns = connections.get(binding_id)
        if binding_conns:
            binding_conns.pop(chat_id, None)
            if not binding_conns:
                connections.pop(binding_id, None)
