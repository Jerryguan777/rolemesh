"""FastAPI application for the RoleMesh WebUI."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import nats
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nats.js.api import StreamConfig

from webui import auth, ws
from webui.config import NATS_URL, WEB_UI_DIST, WEB_UI_HOST, WEB_UI_PORT

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


@app.websocket("/ws/chat")
async def websocket_chat(
    websocket: WebSocket,
    binding_id: str = "",
    token: str = "",
) -> None:
    if not binding_id or not token:
        await websocket.close(code=1008, reason="Missing binding_id or token")
        return
    await ws.handle_ws(websocket, binding_id, token)


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
