"""Integration tests for per-tenant credential injection in reverse_proxy.

Exercises the full request path:

    client -> reverse proxy -> token verify -> CredentialResolver
                            -> tenant_model_credentials -> Fernet decrypt
                            -> upstream LLM API (here: a local echo
                               server that records the injected headers)

Identity comes from a signed token in the request path
(``/proxy/<token>/<provider>/...``), exactly as the orchestrator injects
it into the agent's ``ANTHROPIC_BASE_URL``. Each tenant gets its own
minted token; the proxy verifies it and reads the tenant from the
claims. This is the real production code path, not a mock.

Each test names the mutation it pins.
"""

from __future__ import annotations

import socket
import uuid
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aiohttp import web
from cryptography.fernet import Fernet

from rolemesh.auth.credential_vault import CredentialVault
from rolemesh.db import _get_admin_pool, create_tenant
from rolemesh.egress.credentials import CredentialResolver
from rolemesh.egress.reverse_proxy import (
    _bedrock_upstream,
    start_credential_proxy,
)
from rolemesh.egress.token_identity import Identity, TokenAuthority

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = [pytest.mark.usefixtures("test_db"), pytest.mark.asyncio]

# Shared signing secret for the test proxy's TokenAuthority.
_TOKEN_SECRET = "db-cred-test-secret-16+chars"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault(Fernet.generate_key())


class _Echo:
    """Real aiohttp upstream that records every received request."""

    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []
        self._runner: web.AppRunner | None = None
        self.port: int = 0

    async def start(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            self.received.append(
                {
                    "method": request.method,
                    "path": request.path_qs,
                    "headers": {k: v for k, v in request.headers.items()},
                }
            )
            return web.Response(status=200, text="ok")

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def echo() -> AsyncGenerator[_Echo, None]:
    e = _Echo()
    await e.start()
    yield e
    await e.stop()


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_AUTHORITY = TokenAuthority(secret=_TOKEN_SECRET, ttl_seconds=3600)


async def _start_proxy(
    *,
    vault: CredentialVault,
) -> tuple[web.AppRunner, int]:
    """Boot a proxy wired with the shared token authority."""
    resolver = CredentialResolver(vault)
    port = _pick_free_port()
    runner = await start_credential_proxy(
        port=port,
        host="127.0.0.1",
        credential_resolver=resolver,
        token_authority=_AUTHORITY,
    )
    return runner, port


def _identity(tenant_id: str, suffix: str = "1") -> Identity:
    return Identity(
        tenant_id=tenant_id,
        coworker_id=f"cw-{suffix}",
        user_id="",
        conversation_id="",
        job_id="",
        container_name=f"container-{suffix}",
    )


def _token_for(tenant_id: str, suffix: str = "1") -> str:
    """Mint a token the test proxy will accept for *tenant_id*."""
    return _AUTHORITY.mint(_identity(tenant_id, suffix))


async def _new_tenant(slug_hint: str) -> str:
    t = await create_tenant(
        name=f"T-{slug_hint}",
        slug=f"{slug_hint}-{uuid.uuid4().hex[:8]}",
    )
    return t.id


async def _write_cred(
    tenant_id: str, provider: str, blob: bytes
) -> None:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_model_credentials "
            "(tenant_id, provider, credential_data) "
            "VALUES ($1::uuid, $2, $3)",
            tenant_id, provider, blob,
        )


def _proxy_url(port: int, token: str, provider: str, path: str = "v1/messages") -> str:
    """Build the token-bearing reverse-proxy URL the SDKs would form."""
    return f"http://127.0.0.1:{port}/proxy/{token}/{provider}/{path}"


# ---------------------------------------------------------------------------
# Test 1 — per-tenant API key injection (cross-tenant isolation)
# ---------------------------------------------------------------------------


async def test_per_tenant_api_key_injected(
    vault: CredentialVault, echo: _Echo, monkeypatch: pytest.MonkeyPatch
):
    """Pin: tenant A's request gets A's key; tenant B's gets B's.

    Mutation: caching by ``provider`` alone (dropping tenant_id from
    the key) makes B's request reuse A's cached cred — B's expected
    header value never reaches the echo server.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{echo.port}")

    tenant_a = await _new_tenant("alpha")
    tenant_b = await _new_tenant("beta")
    await _write_cred(
        tenant_a, "anthropic", vault.encrypt_json({"api_key": "K_ALPHA"}),
    )
    await _write_cred(
        tenant_b, "anthropic", vault.encrypt_json({"api_key": "K_BETA"}),
    )

    runner, port = await _start_proxy(vault=vault)
    url_a = _proxy_url(port, _token_for(tenant_a, "a"), "anthropic")
    url_b = _proxy_url(port, _token_for(tenant_b, "b"), "anthropic")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url_a, data=b"{}") as resp:
                assert resp.status == 200, await resp.text()
            async with session.post(url_b, data=b"{}") as resp:
                assert resp.status == 200, await resp.text()
    finally:
        await runner.cleanup()

    assert len(echo.received) == 2
    keys = [r["headers"].get("x-api-key") for r in echo.received]  # type: ignore[union-attr]
    # First request carried tenant A's token; second tenant B's.
    assert keys[0] == "K_ALPHA"
    assert keys[1] == "K_BETA"


# ---------------------------------------------------------------------------
# Test 2 — missing credential returns 401, no silent host-env fallback
# ---------------------------------------------------------------------------


async def test_missing_credential_returns_401_no_silent_fallback(
    vault: CredentialVault, echo: _Echo, monkeypatch: pytest.MonkeyPatch
):
    """Pin: tenant has identity but no DB row -> 401, upstream untouched.

    Sets a host env ``ANTHROPIC_API_KEY`` that the handler MUST NOT
    silently use. Mutation: re-adding fallback (``or os.environ.get(...)``)
    would make the request reach the echo server and assert echo count
    == 0 to fail.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{echo.port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-env-key-must-not-be-used")

    tenant_id = await _new_tenant("nocred")
    # No row written.

    runner, port = await _start_proxy(vault=vault)
    url = _proxy_url(port, _token_for(tenant_id, "n"), "anthropic")
    try:
        async with aiohttp.ClientSession() as session, session.post(url, data=b"{}") as resp:
            assert resp.status == 401
            assert "MISSING_CREDENTIAL" in await resp.text()
    finally:
        await runner.cleanup()

    assert echo.received == [], (
        "fail-closed violated: upstream received a request"
    )


# ---------------------------------------------------------------------------
# Test 3 — invalid identity token returns 401
# ---------------------------------------------------------------------------


async def test_invalid_token_returns_401(
    vault: CredentialVault, echo: _Echo, monkeypatch: pytest.MonkeyPatch
):
    """Pin: a request whose path token fails verification -> 401 UNKNOWN_SOURCE.

    Mutation: defaulting an unverifiable token to any tenant ("dev
    fallback") would let a forged request reach an upstream with
    someone's credential.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{echo.port}")

    tenant_id = await _new_tenant("known")
    await _write_cred(
        tenant_id, "anthropic", vault.encrypt_json({"api_key": "K"}),
    )

    runner, port = await _start_proxy(vault=vault)
    # A real token, tampered in its signature half — must not verify.
    good = _token_for(tenant_id, "k")
    forged = good[:-2] + ("aa" if good[-2:] != "aa" else "bb")
    url = _proxy_url(port, forged, "anthropic")
    try:
        async with aiohttp.ClientSession() as session, session.post(url, data=b"{}") as resp:
            assert resp.status == 401
            assert "UNKNOWN_SOURCE" in await resp.text()
    finally:
        await runner.cleanup()

    assert echo.received == []


# ---------------------------------------------------------------------------
# Test 4 — Anthropic OAuth via cred extras uses Bearer header
# ---------------------------------------------------------------------------


async def test_anthropic_oauth_via_extras_uses_bearer(
    vault: CredentialVault, echo: _Echo, monkeypatch: pytest.MonkeyPatch
):
    """Pin: cred lacking api_key but with extras.oauth_token -> Bearer.

    Mutation: only honouring ``cred["api_key"]`` (dropping the OAuth
    branch) makes the request 401 with empty api_key — echo never
    sees the Bearer header.
    """
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{echo.port}")

    tenant_id = await _new_tenant("oauth")
    await _write_cred(
        tenant_id, "anthropic",
        vault.encrypt_json(
            {"api_key": "", "extras": {"oauth_token": "OAUTH_TOK"}}
        ),
    )

    runner, port = await _start_proxy(vault=vault)
    url = _proxy_url(port, _token_for(tenant_id, "o"), "anthropic")
    try:
        async with aiohttp.ClientSession() as session, session.post(url, data=b"{}") as resp:
            assert resp.status == 200, await resp.text()
    finally:
        await runner.cleanup()

    assert len(echo.received) == 1
    # Normalise header names — aiohttp preserves whatever case the
    # wire carried, which varies between client / server.
    headers_lower = {
        k.lower(): v for k, v in echo.received[0]["headers"].items()  # type: ignore[union-attr]
    }
    assert headers_lower.get("authorization") == "Bearer OAUTH_TOK"
    assert "x-api-key" not in headers_lower


# ---------------------------------------------------------------------------
# Test 5 — Bedrock upstream comes from cred extras, not host env
# ---------------------------------------------------------------------------


async def test_bedrock_upstream_from_cred_extras_not_host_env(
    monkeypatch: pytest.MonkeyPatch,
):
    """Pin: ``_bedrock_upstream`` reads ``cred[extras][region]``, ignores host AWS_REGION.

    Unit-level: the integration path for Bedrock requires DNS-resolving
    a real AWS hostname, which is the wrong thing to exercise in a
    unit test. The pure function carries the contract we care about.

    Mutation: switching to ``os.environ.get("AWS_REGION", ...)`` would
    return the deliberately-different host env value.
    """
    monkeypatch.setenv("AWS_REGION", "us-east-1")  # decoy

    cred = {"api_key": "absk-x", "extras": {"region": "us-west-2"}}
    assert (
        _bedrock_upstream(cred)
        == "https://bedrock-runtime.us-west-2.amazonaws.com"
    )

    # And falls back to the const when extras has no region.
    cred_no_region: dict[str, object] = {"api_key": "absk-x", "extras": {}}
    from rolemesh.core.config import BEDROCK_DEFAULT_REGION
    assert (
        _bedrock_upstream(cred_no_region)
        == f"https://bedrock-runtime.{BEDROCK_DEFAULT_REGION}.amazonaws.com"
    )


# ---------------------------------------------------------------------------
# Test 6 — legacy catch-all route is gone (any path returns 404)
# ---------------------------------------------------------------------------


async def test_root_catchall_route_returns_404(
    vault: CredentialVault,
):
    """Pin: the pre-multi-tenant catch-all is deleted; any unknown path -> 404.

    Mutation: re-registering the route (or accidentally globbing
    ``/{path:.*}`` somewhere) would let requests fall through to a
    handler again and we'd see a non-404 status.
    """
    await _new_tenant("legacy")
    runner, port = await _start_proxy(vault=vault)
    try:
        async with aiohttp.ClientSession() as session:
            # Bare root.
            async with session.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 404
            # Plausible legacy Anthropic path that hit the old
            # catch-all (``app.router.add_route("*", "/{path:.*}",
            # handle_legacy_anthropic)``).
            async with session.post(
                f"http://127.0.0.1:{port}/v1/messages", data=b"{}",
            ) as resp:
                assert resp.status == 404
            # And a random path that the catch-all used to swallow.
            async with session.get(
                f"http://127.0.0.1:{port}/anything/here",
            ) as resp:
                assert resp.status == 404
    finally:
        await runner.cleanup()
