"""WebSocket handler — bridges browser clients to NATS."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from nats.js.api import DeliverPolicy
from starlette.websockets import WebSocketState

from rolemesh.ipc.web_protocol import WebInboundMessage
from webui.auth import validate_token

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

# binding_id -> { chat_id -> [WebSocket, ...] }
connections: dict[str, dict[str, list[WebSocket]]] = {}

_js: JetStreamContext | None = None


def set_jetstream(js: JetStreamContext) -> None:
    """Set the shared JetStream context (called once on startup)."""
    global _js
    _js = js


def _is_valid_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
    except ValueError:
        return False
    return True


async def _broadcast(binding_id: str, chat_id: str, data: dict[str, str]) -> None:
    """Send a JSON message to all WebSocket connections for a chat_id."""
    ws_list = connections.get(binding_id, {}).get(chat_id, [])
    for ws in ws_list:
        with contextlib.suppress(OSError, RuntimeError, WebSocketDisconnect):
            await ws.send_json(data)


async def handle_ws(ws: WebSocket, binding_id: str, token: str, chat_id: str = "") -> None:
    """Handle a single WebSocket connection lifecycle."""
    if not validate_token(binding_id, token):
        await ws.close(code=1008, reason="Invalid token")
        return

    await ws.accept()

    assert _js is not None

    # Use client-provided chat_id if valid UUID, otherwise generate one
    if not chat_id or not _is_valid_uuid(chat_id):
        chat_id = str(uuid.uuid4())
    sender_id = f"web-user-{chat_id[:8]}"

    # Send session info
    await ws.send_json({"type": "session", "chatId": chat_id, "bindingId": binding_id})

    # Register connection (multiple WS per chat_id supported)
    connections.setdefault(binding_id, {}).setdefault(chat_id, []).append(ws)

    # Subscribe to NATS subjects for this connection (deliver_policy=NEW
    # to avoid replaying old messages when reconnecting to an existing chat)
    stream_sub = await _js.subscribe(
        f"web.stream.{binding_id}.{chat_id}",
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )
    typing_sub = await _js.subscribe(
        f"web.typing.{binding_id}.{chat_id}",
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )
    outbound_sub = await _js.subscribe(
        f"web.outbound.{binding_id}.{chat_id}",
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )

    async def _forward_stream() -> None:
        async for msg in stream_sub.messages:
            try:
                data = json.loads(msg.data)
                if data.get("type") == "text":
                    await _broadcast(binding_id, chat_id, {"type": "text", "content": data["content"]})
                elif data.get("type") == "done":
                    await _broadcast(binding_id, chat_id, {"type": "done"})
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
                    await _broadcast(binding_id, chat_id, {"type": "thinking"})
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
                await _broadcast(binding_id, chat_id, {"type": "text", "content": data["text"]})
                await _broadcast(binding_id, chat_id, {"type": "done"})
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

        # Remove this WS from the connection list
        ws_list = connections.get(binding_id, {}).get(chat_id, [])
        if ws in ws_list:
            ws_list.remove(ws)
        if not ws_list:
            connections.get(binding_id, {}).pop(chat_id, None)
        if binding_id in connections and not connections[binding_id]:
            connections.pop(binding_id, None)
