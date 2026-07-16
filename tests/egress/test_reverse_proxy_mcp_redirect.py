"""Reverse-proxy MCP redirect / injected-header survival.

Regression for the bug where an MCP server whose ``/mcp/`` endpoint
307-redirects to ``/mcp`` (FastMCP/Starlette default) lost the
per-server ``extra_headers`` the gateway injects — Authorization +
X-Actor-* — because the gateway used ``allow_redirects=False`` and
passed the bare 307 back to the agent's MCP client, whose follow-up
request went out *without* the injected headers and got a 401.

The fix has two layers, both exercised here:

  * fix B (``_collapse_trailing_slash``): a registered ``…/mcp/`` path is
    normalised to ``/mcp`` so the redirect never fires in the common
    case — the upstream is dialled directly.
  * fix A (``follow_redirects=True``): for any *other* redirect a server
    emits, the gateway follows it and the injected headers ride along
    (aiohttp preserves Authorization on a same-origin redirect).

Driven through a real aiohttp test server (no Docker), mirroring
``test_reverse_proxy_token_routing.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rolemesh.egress.reverse_proxy import (
    get_mcp_registry,
    register_mcp_server,
    start_credential_proxy,
    unregister_mcp_server,
)
from rolemesh.egress.token_identity import Identity, TokenAuthority

pytestmark = pytest.mark.asyncio

_SECRET = "mcp-redirect-test-secret-16+chars"
_SERVICE_TOKEN = "Bearer test-token-redteam"
_ACTOR_ID = "userA"
_ACTOR_ROLE = "member"


class _FakeCredResolver:
    async def resolve(self, tenant_id: str, provider: str) -> dict[str, Any]:
        return {"api_key": "sk-test"}


class _Recorder:
    """Captures every request the stub MCP upstream actually served."""

    def __init__(self) -> None:
        self.served: list[tuple[str, dict[str, str]]] = []


async def _make_mcp_upstream(recorder: _Recorder) -> TestServer:
    """Stub MCP server: ``/redir`` 307s to ``/mcp``; ``/mcp`` requires the
    injected ``Bearer test-token-`` auth header (mirrors the red-team
    JWT-prefix middleware) and records what it saw."""

    async def redir(request: web.Request) -> web.Response:
        return web.Response(status=307, headers={"Location": "/mcp"})

    async def mcp(request: web.Request) -> web.Response:
        recorder.served.append((request.path, dict(request.headers)))
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer test-token-"):
            return web.Response(status=401, text="Unauthorized")
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_route("*", "/redir", redir)
    app.router.add_route("*", "/mcp", mcp)
    server = TestServer(app)
    await server.start_server()
    return server


def _identity(tenant: str = "t1") -> Identity:
    return Identity(tenant, "cow", "usr", "conv", "job1", "rolemesh-x-1")


async def _client(proxy_runner: web.AppRunner) -> TestClient:
    client = TestClient(TestServer(proxy_runner.app))
    await client.start_server()
    return client


async def _proxy(authority: TokenAuthority) -> web.AppRunner:
    return await start_credential_proxy(
        0,
        credential_resolver=_FakeCredResolver(),  # type: ignore[arg-type]
        token_authority=authority,
    )


def _register(origin: str) -> None:
    register_mcp_server(
        "t1",
        "srv",
        origin,
        headers={
            "Authorization": _SERVICE_TOKEN,
            "X-Actor-Id": _ACTOR_ID,
            "X-Actor-Role": _ACTOR_ROLE,
        },
        auth_mode="service",
    )


async def test_redirect_preserves_injected_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fix A: an upstream redirect is followed by the gateway and the
    injected Authorization + X-Actor headers ride along, so the agent
    sees 200 — not the bare 307 that used to leak through unauthenticated.
    """
    recorder = _Recorder()
    upstream = await _make_mcp_upstream(recorder)
    origin = str(upstream._root).rstrip("/")
    authority = TokenAuthority(secret=_SECRET, ttl_seconds=3600)
    runner = await _proxy(authority)
    client = await _client(runner)
    _register(origin)
    try:
        token = authority.mint(_identity())
        # Hit a path that genuinely redirects (not the trailing-slash
        # case fix B pre-empts) so the redirect-follow is exercised.
        resp = await client.post(f"/mcp-proxy/{token}/srv/redir", data=b"{}")
        assert resp.status == 200
        assert await resp.text() == "ok"

        # The final (post-redirect) request reached /mcp WITH the injected
        # service token and actor headers.
        assert recorder.served, "upstream /mcp was never reached"
        path, headers = recorder.served[-1]
        assert path == "/mcp"
        assert headers.get("Authorization") == _SERVICE_TOKEN
        assert headers.get("X-Actor-Id") == _ACTOR_ID
        assert headers.get("X-Actor-Role") == _ACTOR_ROLE
    finally:
        unregister_mcp_server("t1", "srv")
        await client.close()
        await runner.cleanup()
        await upstream.close()


async def test_trailing_slash_collapsed_no_redirect() -> None:
    """fix B: a request to ``/mcp/`` is normalised to ``/mcp`` so the
    upstream is dialled directly — a single served request, no redirect
    round-trip — and the injected headers are present."""
    recorder = _Recorder()
    upstream = await _make_mcp_upstream(recorder)
    origin = str(upstream._root).rstrip("/")
    authority = TokenAuthority(secret=_SECRET, ttl_seconds=3600)
    runner = await _proxy(authority)
    client = await _client(runner)
    _register(origin)
    try:
        token = authority.mint(_identity())
        resp = await client.post(f"/mcp-proxy/{token}/srv/mcp/", data=b"{}")
        assert resp.status == 200

        # Exactly one served request, straight to /mcp — the trailing
        # slash was collapsed before dialling, so no 307 was needed.
        assert len(recorder.served) == 1
        path, headers = recorder.served[0]
        assert path == "/mcp"
        assert headers.get("Authorization") == _SERVICE_TOKEN
        assert headers.get("X-Actor-Id") == _ACTOR_ID
    finally:
        unregister_mcp_server("t1", "srv")
        await client.close()
        await runner.cleanup()
        await upstream.close()


async def test_collapse_trailing_slash_unit() -> None:
    from rolemesh.egress.reverse_proxy import _collapse_trailing_slash

    assert _collapse_trailing_slash("/mcp/") == "/mcp"
    assert _collapse_trailing_slash("/mcp") == "/mcp"
    assert _collapse_trailing_slash("/mcp//") == "/mcp"
    # Root must be preserved, not emptied.
    assert _collapse_trailing_slash("/") == "/"
    assert _collapse_trailing_slash("") == ""
    # Registry stays clean across the test module.
    assert ("t1", "srv") not in get_mcp_registry()
