"""Shared scaffolding for the red-team sandbox MCP servers.

⚠️ TEST / RED-TEAM ONLY — NOT FOR PRODUCTION.

These servers are deliberately-vulnerable targets used by the promptfoo
red-teaming stage. They are templated on ``tests/mock_mcp_server.py``
(FastMCP + streamable-http + a JWT-prefix middleware + uvicorn) and add:

  * a JWT-prefix auth middleware (accepts ``Bearer test-token-*``, same
    shape as the mock), and
  * a *self-asserted actor* read from the ``X-Actor-Id`` / ``X-Actor-Role``
    request headers.

Why the actor headers matter (the load-bearing fact for the whole rig):
RoleMesh's credential proxy injects a server's static ``extra_headers``
verbatim onto the upstream request (``reverse_proxy.py`` —
``fwd_headers.update(server_headers)``) and *unconditionally strips*
``X-RoleMesh-User-Id`` before forwarding. So under ``auth_mode=service``
the ONLY identity signal that can reach one of these servers is whatever
was registered into ``extra_headers``. The seed registers each server with
a fixed ``X-Actor-Id`` / ``X-Actor-Role`` (e.g. ``userA`` / ``member``),
and these helpers read them back. Every "is this caller allowed?" decision
below is intentionally *absent* — that absence is the BOLA/BFLA target.

Do not add real authorization here. The point is that the data of OTHER
actors/tenants is reachable; whether RoleMesh stops the agent from asking
for it is the promptfoo stage's concern, not this server's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import Response

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

# Header names the seed injects via the MCP server's static extra_headers.
ACTOR_ID_HEADER = "x-actor-id"
ACTOR_ROLE_HEADER = "x-actor-role"

# Fallbacks used only when a request arrives with no actor headers (e.g. a
# direct curl during local debugging). The seeded default actor is userA.
DEFAULT_ACTOR_ID = "userA"
DEFAULT_ACTOR_ROLE = "member"


class JWTAuthMiddleware:
    """Validate the Authorization header by prefix only.

    Mirrors ``tests/mock_mcp_server.py``: accepts any token that starts
    with ``Bearer test-token-``. It does NOT parse a JWT — the token is an
    opaque service credential injected by the credential proxy. (Trying to
    smuggle a per-user ``sub`` through here was rejected during design: the
    token is static and shared, so it carries no per-user identity. Identity
    is the X-Actor-* headers instead.)
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        server_name: str,
        exempt_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self._logger = logging.getLogger(server_name)
        # Paths that bypass auth — used by fetch-mcp to expose an
        # unauthenticated "internal" target for the SSRF scenario.
        self._exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if any(request.url.path.startswith(p) for p in self._exempt_prefixes):
            await self.app(scope, receive, send)
            return
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer test-token-"):
            response = Response(
                "Unauthorized: invalid or missing JWT token", status_code=401
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def actor_of(server: FastMCP) -> tuple[str, str]:
    """Return ``(actor_id, actor_role)`` for the current request.

    Reads the self-asserted ``X-Actor-Id`` / ``X-Actor-Role`` headers off
    the live request. Falls back to the seeded default actor when absent so
    a bare debugging request still resolves to *someone*.

    This is the identity the (intentionally missing) authorization checks
    would have used. Callers MUST NOT treat a returned role as trustworthy
    — it is whatever the proxy was told to inject; that is the BFLA target.
    """
    ctx = server.get_context()
    actor_id = DEFAULT_ACTOR_ID
    actor_role = DEFAULT_ACTOR_ROLE
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
    except (AttributeError, ValueError, LookupError):
        request = None
    if request is not None:
        headers = getattr(request, "headers", {})
        actor_id = headers.get(ACTOR_ID_HEADER, DEFAULT_ACTOR_ID)
        actor_role = headers.get(ACTOR_ROLE_HEADER, DEFAULT_ACTOR_ROLE)
    return actor_id, actor_role


def build_app(
    mcp: FastMCP,
    *,
    server_name: str,
    exempt_prefixes: tuple[str, ...] = (),
) -> Starlette:
    """Wrap the FastMCP streamable-http app with the JWT-prefix middleware."""
    http_app = mcp.streamable_http_app()
    http_app.add_middleware(
        JWTAuthMiddleware,
        server_name=server_name,
        exempt_prefixes=exempt_prefixes,
    )
    return http_app


def run(
    mcp: FastMCP,
    *,
    server_name: str,
    port: int,
    exempt_prefixes: tuple[str, ...] = (),
) -> None:
    """Boot the server under uvicorn (matches the mock's __main__ shape)."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger(server_name).info(
        "Starting red-team MCP %r on http://0.0.0.0:%d/mcp", server_name, port
    )
    uvicorn.run(
        build_app(mcp, server_name=server_name, exempt_prefixes=exempt_prefixes),
        host="0.0.0.0",
        port=port,
    )
