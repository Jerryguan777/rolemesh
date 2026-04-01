"""Mock MCP server for end-to-end testing.

Runs on port 9100 with streamable-http transport at /mcp.
Validates JWT tokens (accepts tokens starting with 'test-token-').
Exposes two tools: echo and get_server_info.

Usage:
    python tests/mock_mcp_server.py
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("mock-mcp-server")

mcp = FastMCP(
    "mock-mcp-server",
    host="0.0.0.0",
    port=9100,
)


@mcp.tool()
def echo(message: str) -> str:
    """Echo back the input message."""
    return f"Echo: {message}"


@mcp.tool()
def get_server_info() -> dict:
    """Get information about this MCP server."""
    return {"name": "mock-mcp-server", "version": "1.0.0", "status": "running"}


class JWTAuthMiddleware:
    """Simple middleware that validates JWT tokens in the Authorization header.

    Accepts any token that starts with 'test-token-'.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        auth_header = request.headers.get("authorization", "")

        if not auth_header.startswith("Bearer test-token-"):
            response = Response("Unauthorized: invalid or missing JWT token", status_code=401)
            await response(scope, receive, send)
            return

        logger.info("JWT token validated: %s", auth_header[:30] + "...")
        await self.app(scope, receive, send)


def create_app() -> Starlette:
    """Create the Starlette app with JWT auth middleware wrapping the MCP streamable-http app."""
    http_app = mcp.streamable_http_app()
    http_app.add_middleware(JWTAuthMiddleware)
    return http_app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("Starting mock MCP server on http://0.0.0.0:9100/mcp")

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=9100, log_level="info")
