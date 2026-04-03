"""FastAPI application for the RoleMesh WebUI."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import nats
from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nats.js.api import StreamConfig

from webui import auth, ws
from webui.config import NATS_URL, WEB_UI_DIST, WEB_UI_HOST, WEB_UI_PORT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.responses import Response

_nc: nats.aio.client.Client | None = None

# Stream max age: 1 hour in seconds
_STREAM_MAX_AGE_S = 3600.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle."""
    global _nc

    # Connect to NATS
    _nc = await nats.connect(NATS_URL, max_reconnect_attempts=3, reconnect_time_wait=1)
    js = _nc.jetstream()

    # Ensure web-ipc stream exists
    await js.add_stream(
        StreamConfig(
            name="web-ipc",
            subjects=["web.>"],
            max_age=_STREAM_MAX_AGE_S,
        )
    )

    ws.set_jetstream(js)

    # Load web bindings from DB
    await auth.init_auth()

    yield

    # Shutdown
    await auth.close_auth()
    if _nc is not None:
        await _nc.close()
        _nc = None


app = FastAPI(lifespan=lifespan)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/conversations")
async def list_conversations(
    binding_id: str = Query(""),
    token: str = Query(""),
) -> JSONResponse:
    """Return conversation list for a web binding."""
    if not binding_id or not token or not auth.validate_token(binding_id, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pool = auth.get_pool()
    if pool is None:
        return JSONResponse({"error": "Database unavailable"}, status_code=503)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.channel_chat_id as chat_id,
                   (SELECT content FROM messages m
                    WHERE m.conversation_id = c.id AND m.is_from_me = FALSE
                    ORDER BY m.timestamp LIMIT 1) as first_msg,
                   (SELECT MAX(m.timestamp) FROM messages m
                    WHERE m.conversation_id = c.id) as updated_at
            FROM conversations c
            WHERE c.channel_binding_id = $1::uuid
              AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
            ORDER BY updated_at DESC NULLS LAST
            """,
            binding_id,
        )

    result = []
    for row in rows:
        first_msg = row["first_msg"] or ""
        title = first_msg[:30] + ("..." if len(first_msg) > 30 else "") if first_msg else "New conversation"
        updated_at = row["updated_at"].isoformat() if row["updated_at"] else ""
        result.append({"chatId": row["chat_id"], "title": title, "updatedAt": updated_at})

    return JSONResponse(result)


@app.get("/api/conversations/{chat_id}/messages")
async def get_messages(
    chat_id: str,
    binding_id: str = Query(""),
    token: str = Query(""),
) -> JSONResponse:
    """Return message history for a conversation."""
    if not binding_id or not token or not auth.validate_token(binding_id, token):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pool = auth.get_pool()
    if pool is None:
        return JSONResponse({"error": "Database unavailable"}, status_code=503)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.content, m.timestamp, m.is_from_me, m.is_bot_message
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE c.channel_binding_id = $1::uuid
              AND c.channel_chat_id = $2
            ORDER BY m.timestamp
            """,
            binding_id,
            chat_id,
        )

    result = []
    for row in rows:
        role = "assistant" if row["is_from_me"] or row["is_bot_message"] else "user"
        content = row["content"] or ""
        # Strip internal tags from assistant messages
        if role == "assistant":
            content = re.sub(r"<internal>[\s\S]*?</internal>", "", content).strip()
        ts = row["timestamp"].isoformat() if row["timestamp"] else ""
        result.append({"role": role, "content": content, "timestamp": ts})

    return JSONResponse(result)


@app.websocket("/ws/chat")
async def websocket_chat(
    websocket: WebSocket,
    binding_id: str = "",
    token: str = "",
    chat_id: str = "",
) -> None:
    if not binding_id or not token:
        await websocket.close(code=1008, reason="Missing binding_id or token")
        return
    await ws.handle_ws(websocket, binding_id, token, chat_id)


# ---------------------------------------------------------------------------
# Admin API stubs (features moved from agent IPC, not yet implemented)
# ---------------------------------------------------------------------------

_NOT_IMPLEMENTED = JSONResponse({"error": "Not yet implemented"}, status_code=501)


@app.post("/api/admin/conversations/register")
async def admin_register_conversation() -> Response:
    return _NOT_IMPLEMENTED


@app.post("/api/admin/conversations/refresh")
async def admin_refresh_conversations() -> Response:
    return _NOT_IMPLEMENTED


@app.post("/api/admin/users/invite")
async def admin_invite_user() -> Response:
    return _NOT_IMPLEMENTED


@app.post("/api/admin/agents/{agent_id}/assign")
async def admin_assign_agent(agent_id: str) -> Response:
    return _NOT_IMPLEMENTED


# Mount static files if the dist directory exists
_dist = Path(WEB_UI_DIST)
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str) -> FileResponse:
        """Serve SPA — all non-API/WS routes return index.html."""
        file_path = _dist / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(_dist / "index.html"))


def main() -> None:
    """Run the WebUI server via uvicorn."""
    import uvicorn

    uvicorn.run(
        "webui.main:app",
        host=WEB_UI_HOST,
        port=WEB_UI_PORT,
        log_level="info",
    )
