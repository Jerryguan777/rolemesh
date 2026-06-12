"""Reverse-proxy token-identity routing (token-identity refactor).

Drives ``start_credential_proxy`` through a real aiohttp test server
(no Docker) with stub upstream + credential resolver + identity sources,
and asserts the dual-run route disambiguation:

  * ``/proxy/<valid-token>/anthropic/...`` -> identity from the token,
    provider parsed as the segment AFTER the token.
  * ``/proxy/anthropic/...`` (no token) -> identity from the source-IP
    resolver, provider is the first segment (pre-refactor shape).
  * ``/proxy/<garbage>/anthropic/...`` with NO ip identity -> 401.

The stub upstream is wired via ANTHROPIC_BASE_URL so no real network is
touched; we assert on which tenant_id the credential resolver was asked
for, which is the whole point of identity resolution.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rolemesh.egress.reverse_proxy import start_credential_proxy
from rolemesh.egress.token_identity import Identity, TokenAuthority

pytestmark = pytest.mark.asyncio

_SECRET = "routing-test-secret-16+chars"


class _FakeCredResolver:
    """Records the (tenant_id, provider) it was asked for; returns a key."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, tenant_id: str, provider: str) -> dict[str, Any]:
        self.calls.append((tenant_id, provider))
        return {"api_key": "sk-test"}


async def _make_upstream() -> TestServer:
    """A stub 'Anthropic' that echoes the path it received."""
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=200, text=f"upstream-path={request.path}")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _identity(tenant: str, job: str = "job1") -> Identity:
    return Identity(tenant, "cow", "usr", "conv", job, "rolemesh-x-1")


async def _client(proxy_runner: web.AppRunner) -> TestClient:
    # start_credential_proxy already started its own site; wrap its app
    # in a TestClient bound to a fresh server for request dispatch.
    client = TestClient(TestServer(proxy_runner.app))
    await client.start_server()
    return client


async def test_token_route_uses_token_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = await _make_upstream()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", str(upstream._root))

    cred = _FakeCredResolver()
    authority = TokenAuthority(secret=_SECRET, ttl_seconds=3600)
    runner = await start_credential_proxy(
        0,
        credential_resolver=cred,  # type: ignore[arg-type]
        token_authority=authority,
    )
    client = await _client(runner)
    try:
        token = authority.mint(_identity("token-tenant", job="jX"))
        resp = await client.post(f"/proxy/{token}/anthropic/v1/messages", data=b"{}")
        assert resp.status == 200
        body = await resp.text()
        # Provider was parsed as the segment AFTER the token; the
        # upstream saw the stripped path.
        assert "/v1/messages" in body
        # Credential lookup used the token's tenant.
        assert cred.calls == [("token-tenant", "anthropic")]
    finally:
        await client.close()
        await runner.cleanup()
        await upstream.close()


async def test_no_token_is_401() -> None:
    """First segment isn't a valid token → no identity → 401, and the
    credential resolver is never consulted (fail-closed)."""
    cred = _FakeCredResolver()
    authority = TokenAuthority(secret=_SECRET, ttl_seconds=3600)
    runner = await start_credential_proxy(
        0,
        credential_resolver=cred,  # type: ignore[arg-type]
        token_authority=authority,
    )
    client = await _client(runner)
    try:
        # 'anthropic' is not a token; with no IP fallback this is 401.
        resp = await client.post("/proxy/anthropic/v1/messages", data=b"{}")
        assert resp.status == 401
        assert cred.calls == []
    finally:
        await client.close()
        await runner.cleanup()


async def test_invalid_token_is_401() -> None:
    cred = _FakeCredResolver()
    authority = TokenAuthority(secret=_SECRET, ttl_seconds=3600)
    runner = await start_credential_proxy(
        0,
        credential_resolver=cred,  # type: ignore[arg-type]
        token_authority=authority,
    )
    client = await _client(runner)
    try:
        resp = await client.post("/proxy/not-a-real-token/anthropic/v1/messages", data=b"{}")
        assert resp.status == 401
        assert cred.calls == []
    finally:
        await client.close()
        await runner.cleanup()
