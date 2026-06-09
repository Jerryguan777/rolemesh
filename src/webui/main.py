"""FastAPI application for the RoleMesh WebUI."""
# ruff: noqa: I001
# See rolemesh.main for the import-order rationale. bootstrap must
# run before webui.config + peers capture module-level env values.

from __future__ import annotations

# Side-effect import: runs load_env() so ``.env`` lands in os.environ
# BEFORE webui/config (DATABASE_URL, NATS_URL, WEB_UI_PORT, WS_TICKET_SECRET,
# ...) captures module-level values. Without this load, env-sourced config
# would come through as defaults/"" for any operator who wrote them into
# ``.env`` rather than exporting them in the process environment.
import rolemesh.bootstrap  # noqa: F401

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import nats
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nats.js.api import StreamConfig
from rolemesh.auth.bootstrap_users import init_bootstrap_users
from rolemesh.db import (
    _get_pool,
    close_database,
    init_database,
)
from webui import auth
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

    # Initialize the shared DB pool (used by both admin API and web binding auth)
    await _init_db()

    # Load web bindings using the shared pool
    await auth.init_auth(_get_pool())
    await auth.init_auth_provider()

    # BOOTSTRAP_USERS multi-user fast-path (§5.2.1). Parsing happens
    # once at startup so a malformed spec fails the process boot
    # instead of intermittently failing requests. The function is a
    # no-op when the env var is unset, and aborts boot when set under
    # ROLEMESH_ENV=production.
    init_bootstrap_users()

    # Env-seed the first platform_admin (managed / IaC convenience).
    # No-op unless ROLEMESH_SEED_ADMIN_EMAIL is set; idempotent and
    # self-disabling. The canonical interactive path is the
    # ``rolemesh-admin create-admin`` CLI.
    from rolemesh.admin.core import ensure_seed_admin

    await ensure_seed_admin()

    # Initialize TokenVault for OIDC token mirroring (mirrors orchestrator init).
    # This is per-process: orchestrator and webui each hold their own vault.
    from rolemesh.auth.token_vault import create_vault_from_env
    from webui import oidc_routes

    _vault = await create_vault_from_env()
    if _vault is not None:
        oidc_routes.set_token_vault(_vault)

    # v1.1 §8.1: install the LLM CredentialVault singleton. Fails loud
    # if ``CREDENTIAL_VAULT_KEY`` is unset — INV-VAULT-1. Done here
    # (not at import time) so test apps that skip the lifespan don't
    # accidentally depend on the env var.
    from rolemesh.auth.credential_vault import (
        create_credential_vault_from_env,
        set_credential_vault,
    )

    set_credential_vault(create_credential_vault_from_env())

    # v1.1 §7: wire the coworker hot-reload publisher. PATCH on
    # /api/v1/coworkers/{id} (model_id change) emits
    # ``web.coworker.restart`` via JetStream so the orchestrator
    # re-reads the row without a full restart.
    from webui.v1 import coworker_events, mcp_events, run_events, ws_stream

    # Wire the MCP-registry hot-reload publisher. When an operator
    # creates/updates an MCP server, the v1 handler emits one
    # ``egress.mcp.changed`` event so the gateway can update routes
    # without an orchestrator restart. Uses core NATS (not JetStream) —
    # the broadcast is at-most-once but the gateway's snapshot fetch on
    # boot handles missed deltas as a backstop.
    mcp_events.set_mcp_publisher(_nc)

    coworker_events.set_jetstream(js)
    # v1.1 §4 (INV-6): wire the run-cancel publisher. POST
    # /api/v1/runs/{id}/cancel emits ``web.run.cancel.{run_id}`` so
    # the orchestrator stops the container and the lifecycle helper
    # writes the terminal UPDATE — the webui never writes
    # ``status='cancelled'`` directly (avoids ghost containers).
    run_events.set_jetstream(js)
    # v1.1 §4: wire the WS /api/v1/conversations/{id}/stream
    # endpoint's JetStream context. The route itself is mounted
    # at app-build time (below), but the JS handle is what each
    # connection uses to publish ``web.inbound.*`` / subscribe
    # to ``web.stream.*``.
    ws_stream.set_jetstream(js)

    yield

    # Shutdown
    coworker_events.set_jetstream(None)
    run_events.set_jetstream(None)
    ws_stream.set_jetstream(None)
    mcp_events.set_mcp_publisher(None)
    set_credential_vault(None)
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


# v1 router: new prefixed surface introduced by webui-backend v1.1.
from webui.api_v1 import router as api_v1_router  # noqa: E402
from webui.v1.errors import install_error_handler  # noqa: E402

# Flatten ErrorResponseException -> root JSON body for every /api/v1
# 4xx so the typed client can ``narrow`` on the {code, message,
# details?} envelope. Without this, FastAPI's default handler nests
# the envelope inside ``{"detail": ...}`` and the codegen-generated
# TS client can't decode it.
install_error_handler(app)
app.include_router(api_v1_router)

# v1 WebSocket stream. PR-B (2026-05-31) removed the legacy
# ``/ws/chat`` endpoint after migrating the Stop button into the v1
# ``request.stop`` client frame — the SPA now uses a single WS per
# chat-panel for both streaming and Stop.
from webui.v1.ws_stream import register_routes as _register_v1_ws  # noqa: E402

_register_v1_ws(app)

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
