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

from rolemesh.db import pg
from rolemesh.ipc.web_protocol import WebInboundMessage
from webui import auth
from webui.auth import BOOTSTRAP_USER_ID

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


async def _broadcast(binding_id: str, chat_id: str, data: dict[str, object]) -> None:
    """Send a JSON message to all WebSocket connections for a chat_id."""
    ws_list = connections.get(binding_id, {}).get(chat_id, [])
    for ws in ws_list:
        with contextlib.suppress(OSError, RuntimeError, WebSocketDisconnect):
            await ws.send_json(data)


async def handle_ws(ws: WebSocket, agent_id: str, token: str, chat_id: str = "") -> None:
    """Handle a single WebSocket connection lifecycle."""
    # 1. Authenticate via JWT / bootstrap token
    user = await auth.authenticate_ws(token)
    if user is None:
        await ws.close(code=1008, reason="Invalid token")
        return

    # 2. Look up the coworker (agent)
    try:
        coworker = await pg.get_coworker(agent_id, tenant_id=user.tenant_id)
    except Exception:  # asyncpg.DataError for invalid UUID  # noqa: BLE001
        coworker = None
    if coworker is None:
        await ws.close(code=4004, reason="Agent not found")
        return

    # 3. Check assignment — owners/admins can access any agent
    if user.role not in ("owner", "admin"):
        assigned = await pg.get_agents_for_user(user.user_id)
        assigned_ids = {c.id for c in assigned}
        if coworker.id not in assigned_ids:
            await ws.close(code=4003, reason="Not assigned to this agent")
            return

    # 4. Look up web binding for this coworker
    binding = await pg.get_channel_binding_for_coworker(agent_id, "web")
    if binding is None:
        await ws.close(code=4004, reason="Web binding not found")
        return
    binding_id = binding.id

    await ws.accept()

    assert _js is not None

    # Use client-provided chat_id if valid UUID, otherwise generate one
    if not chat_id or not _is_valid_uuid(chat_id):
        chat_id = str(uuid.uuid4())

    sender_id = user.user_id if user.user_id != BOOTSTRAP_USER_ID else f"web-user-{chat_id[:8]}"

    # 5. Find or create conversation
    conv = await pg.get_conversation_by_binding_and_chat(binding_id, chat_id)
    if conv is None:
        conv = await pg.create_conversation(
            tenant_id=user.tenant_id,
            coworker_id=coworker.id,
            channel_binding_id=binding_id,
            channel_chat_id=chat_id,
            user_id=user.user_id if user.user_id != BOOTSTRAP_USER_ID else None,
            # Web is 1:1 direct chat — no trigger pattern gating. The pg default
            # of True is correct for telegram/slack group chats but would cause
            # the orchestrator to ignore plain "hello" messages from web users.
            requires_trigger=False,
        )
    elif conv.user_id is None and user.user_id != BOOTSTRAP_USER_ID:
        await pg.update_conversation_user_id(conv.id, user.user_id)

    # Send session info (include binding_id for NATS subscription subjects)
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
                kind = data.get("type")
                if kind == "text":
                    await _broadcast(binding_id, chat_id, {"type": "text", "content": data["content"]})
                elif kind == "done":
                    await _broadcast(binding_id, chat_id, {"type": "done"})
                elif kind == "status":
                    # content is a JSON-encoded progress payload; unwrap and
                    # forward as a typed status frame to the browser. Spread
                    # payload first so a literal "type": "status" always wins.
                    payload = json.loads(data.get("content", "{}"))
                    out = {**payload, "type": "status"}
                    await _broadcast(binding_id, chat_id, out)
                elif kind == "safety_blocked":
                    # Safety-block forwarded as its own frame so the client
                    # can render a distinct bubble (red shield) rather than
                    # conflating it with an assistant text reply.
                    payload = json.loads(data.get("content", "{}"))
                    out = {**payload, "type": "safety_blocked"}
                    await _broadcast(binding_id, chat_id, out)
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
                    sender_name=user.name or "Web User",
                    text=data["content"],
                    timestamp=datetime.now(UTC).isoformat(),
                    msg_id=str(uuid.uuid4()),
                )
                await _js.publish(
                    f"web.inbound.{binding_id}",
                    inbound.to_bytes(),
                )
            elif data.get("type") == "stop":
                # User clicked Stop. Interrupt the active agent turn.
                # Do NOT use data.get("chat_id") / data.get("binding_id")
                # from the payload — always use the authenticated
                # binding_id/chat_id from the WebSocket handshake to
                # prevent IDOR from a compromised or malicious client.
                await _js.publish(f"web.stop.{binding_id}.{chat_id}", b"{}")
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
