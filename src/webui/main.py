"""FastAPI application for the RoleMesh WebUI."""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import nats
from fastapi import FastAPI, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nats.js.api import StreamConfig

from rolemesh.db import pg
from rolemesh.db.pg import _get_pool, close_database, init_database
from webui import auth, ws
from webui.admin import router as admin_router
from webui.config import (
    CORS_ORIGINS,
    DATABASE_URL,
    NATS_URL,
    WEB_UI_DIST,
    WEB_UI_HOST,
    WEB_UI_PORT,
)


async def _init_db() -> None:
    await init_database(DATABASE_URL)


async def _close_db() -> None:
    await close_database()

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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

    # Initialize the shared DB pool (used by both admin API and web binding auth)
    await _init_db()

    # Load web bindings using the shared pool
    await auth.init_auth(_get_pool())
    await auth.init_auth_provider()

    # Initialize TokenVault for OIDC token mirroring (mirrors orchestrator init).
    # This is per-process: orchestrator and webui each hold their own vault.
    from rolemesh.auth.token_vault import create_vault_from_env
    from webui import oidc_routes

    _vault = await create_vault_from_env()
    if _vault is not None:
        oidc_routes.set_token_vault(_vault)

    # Approval engine for the WebUI's decide endpoint. The orchestrator
    # process also owns an engine of its own for IPC events; they do not
    # share in-memory state, only the DB. The decide endpoint returns
    # 503 unless this is wired up, so the admin UI surfaces the missing
    # configuration instead of silently 404'ing.
    from rolemesh.approval.engine import ApprovalEngine
    from rolemesh.approval.notification import NotificationTargetResolver
    from rolemesh.db.pg import get_conversation as _pg_get_conv
    from webui import admin as _admin
    from webui.config import WEBUI_BASE_URL

    class _WebuiNoopChannel:
        async def send_to_conversation(
            self, conversation_id: str, text: str
        ) -> None:
            # Notifications are authored by the orchestrator process,
            # which owns the gateway fan-out. The WebUI process handles
            # decide but does not push back to channels itself.
            return

    async def _no_convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    _admin.set_approval_engine(
        ApprovalEngine(
            publisher=js,
            channel_sender=_WebuiNoopChannel(),
            resolver=NotificationTargetResolver(
                get_conversations_for_user_and_coworker=_no_convs,
                get_conversation=_pg_get_conv,
                webui_base_url=WEBUI_BASE_URL or None,
            ),
        )
    )

    yield

    # Shutdown
    await auth.close_auth()
    await _close_db()
    if _nc is not None:
        await _nc.close()
        _nc = None


app = FastAPI(lifespan=lifespan)

# CORS for embedded SaaS scenarios where the browser sends credentials
# (httpOnly refresh cookie) cross-origin. Only enabled when CORS_ORIGINS is set.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _resolve_web_agent(agent_id: str, token: str) -> tuple[str, str] | JSONResponse:
    """Authenticate and resolve the web binding for an agent.

    Returns (binding_id, tenant_id) on success, or a JSONResponse error.
    """
    if not agent_id or not token:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    user = await auth.authenticate_ws(token)
    if user is None:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        coworker = await pg.get_coworker(agent_id)
    except asyncpg.DataError:
        coworker = None
    if coworker is None or coworker.tenant_id != user.tenant_id:
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    binding = await pg.get_channel_binding_for_coworker(agent_id, "web")
    if binding is None:
        return JSONResponse({"error": "Web binding not found"}, status_code=404)
    return binding.id, user.tenant_id


@app.get("/api/conversations")
async def list_conversations(
    agent_id: str = Query(""),
    token: str = Query(""),
) -> JSONResponse:
    """Return conversation list for a web binding."""
    result_or_error = await _resolve_web_agent(agent_id, token)
    if isinstance(result_or_error, JSONResponse):
        return result_or_error
    binding_id, _ = result_or_error

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
    agent_id: str = Query(""),
    token: str = Query(""),
) -> JSONResponse:
    """Return message history for a conversation."""
    result_or_error = await _resolve_web_agent(agent_id, token)
    if isinstance(result_or_error, JSONResponse):
        return result_or_error
    binding_id, _ = result_or_error

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
    agent_id: str = "",
    token: str = "",
    chat_id: str = "",
) -> None:
    if not agent_id or not token:
        await websocket.close(code=1008, reason="Missing agent_id or token")
        return
    await ws.handle_ws(websocket, agent_id, token, chat_id)


# Admin API router
app.include_router(admin_router)

# OIDC PKCE router (only when AUTH_MODE=oidc)
if os.environ.get("AUTH_MODE", "external") == "oidc":
    from webui.oidc_routes import router as oidc_router

    app.include_router(oidc_router)


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
