"""E (identity isolation). Forged identity header must not steer credentials.

RoleMesh's per-user / per-tenant credential isolation is enforced at the
credential proxy (``rolemesh.egress.reverse_proxy``), not in the model. A
container is only half-trusted: once injected / jailbroken it can put any
``X-RoleMesh-User-Id`` it likes on its outbound MCP/LLM requests. The proxy
must therefore derive identity from the VERIFIED signed token in the URL
path (``identity`` from ``TokenAuthority.verify``), never from that header.

These are white-box, deterministic tests: they boot the real
``start_credential_proxy`` app in-process and drive it over real HTTP. The
only fakes are at true boundaries — the upstream MCP/LLM server (an echo),
the token vault, and the credential resolver. No Docker, no DB; sub-second.

The LLM-jailbreak face of this (can a prompt make the agent emit the
header?) belongs to the promptfoo black-box run, which today cannot reach
this code at all because ``redteam/seed.py`` pins ``auth_mode="service"``.
That dead-code gap is exactly why a deterministic white-box pin is needed.

  E7 (MCP)      forged X-RoleMesh-User-Id: userB on a userA-token request
                must NOT fetch userB's OIDC token from the vault.
  E7 (provider) the LLM credential is selected by identity.tenant_id; a
                forged header has no effect (control — pins the path that
                is already correct, and shows the pattern the MCP path
                must follow).
"""

from __future__ import annotations

import socket
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from rolemesh.egress import reverse_proxy as rp
from rolemesh.egress.credentials import MissingCredentialError
from rolemesh.egress.reverse_proxy import register_mcp_server, start_credential_proxy
from rolemesh.egress.token_identity import Identity, TokenAuthority

_USER_ID_HEADER = "X-RoleMesh-User-Id"
_AUTHORITY = TokenAuthority(secret="attack-sim-e7-secret-16+chars", ttl_seconds=3600)


# ---------------------------------------------------------------------------
# In-process boundary fakes
# ---------------------------------------------------------------------------


class _Echo:
    """Real upstream that records the headers each request arrived with."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            self.received.append({"headers": dict(request.headers)})
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

    def last_headers(self) -> dict[str, str]:
        assert self.received, "request never reached the upstream"
        return {k.lower(): v for k, v in self.received[-1]["headers"].items()}


class _StubVault:
    """Token vault keyed by user_id — records which user_id was looked up."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens
        self.lookups: list[str] = []

    async def get_fresh_access_token(self, user_id: str) -> str | None:
        self.lookups.append(user_id)
        return self._tokens.get(user_id)


class _RecordingResolver:
    """Credential resolver that records every (tenant_id, provider) asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, tenant_id: str, provider: str) -> dict[str, Any]:
        self.calls.append((tenant_id, provider))
        return {"api_key": f"KEY-{tenant_id}"}


class _NullResolver:
    """The MCP path must never consult the credential resolver."""

    async def resolve(self, tenant_id: str, provider: str) -> dict[str, Any]:
        raise MissingCredentialError(
            f"resolver must not be called on the MCP path ({tenant_id}/{provider})"
        )


# ---------------------------------------------------------------------------
# Harness helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _token(*, user_id: str, tenant_id: str) -> str:
    """Mint the signed egress token the orchestrator bakes into the proxy URL."""
    return _AUTHORITY.mint(
        Identity(
            tenant_id=tenant_id,
            coworker_id="cw-1",
            user_id=user_id,
            conversation_id="conv-1",
            job_id="job-1",
            container_name="c-1",
        )
    )


async def _start_proxy(resolver: Any) -> tuple[web.AppRunner, int]:
    port = _free_port()
    runner = await start_credential_proxy(
        port=port,
        host="127.0.0.1",
        credential_resolver=resolver,
        token_authority=_AUTHORITY,
        safety_caller=None,  # no-op gate; we are testing identity, not safety
    )
    return runner, port


@pytest.fixture(autouse=True)
def _restore_proxy_globals() -> Any:
    """Snapshot + restore the reverse_proxy module globals these tests mutate
    (``_token_vault`` and ``_mcp_registry``) so they never leak across tests."""
    saved_vault = rp._token_vault
    saved_registry = dict(rp._mcp_registry)
    yield
    rp._token_vault = saved_vault
    rp._mcp_registry.clear()
    rp._mcp_registry.update(saved_registry)


# ---------------------------------------------------------------------------
# E7 — MCP path: forged header must not select another user's vault token
# ---------------------------------------------------------------------------


async def test_E7_mcp_forged_user_id_header_does_not_select_another_users_token() -> None:
    """Attacker: a userA container (holding a valid userA egress token) sends
    an MCP request with ``X-RoleMesh-User-Id: userB`` forged in the headers,
    hoping the proxy fetches userB's OIDC access token from the shared vault
    and forwards it upstream. Defense: identity comes from the verified token
    (identity.user_id == userA), so the vault is asked for userA's token and
    the upstream sees userA's bearer — never userB's."""
    echo = _Echo()
    await echo.start()
    vault = _StubVault({"userA": "token-A", "userB": "token-B"})
    rp.set_token_vault(vault)
    register_mcp_server("acme", f"http://127.0.0.1:{echo.port}", auth_mode="user")
    runner, port = await _start_proxy(_NullResolver())
    try:
        token = _token(user_id="userA", tenant_id="tenant-a")
        url = f"http://127.0.0.1:{port}/mcp-proxy/{token}/acme/mcp"
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, data=b"{}", headers={_USER_ID_HEADER: "userB"}) as resp,
        ):
            await resp.read()

        upstream = echo.last_headers()
        assert upstream.get("authorization") == "Bearer token-A", (
            f"proxy forwarded {upstream.get('authorization')!r}; a forged "
            "X-RoleMesh-User-Id:userB must NOT fetch userB's token — identity "
            "must come from the verified egress token, not the request header"
        )
        # userB's token must never even be looked up.
        assert "userB" not in vault.lookups, (
            f"vault was queried for a forged user_id: {vault.lookups}"
        )
        # The (forgeable) identity header must not leak to the upstream MCP server.
        assert _USER_ID_HEADER.lower() not in upstream
    finally:
        await runner.cleanup()
        await echo.stop()


# ---------------------------------------------------------------------------
# E7 (provider control) — LLM credential keys on the token's tenant, not header
# ---------------------------------------------------------------------------


async def test_E7_provider_credential_selection_ignores_forged_user_id_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the provider (LLM) path already does it right — the credential
    is resolved by identity.tenant_id from the verified token, and the
    X-RoleMesh-User-Id header plays no part in selection. A forged header
    must not change which credential is fetched. This pins the correct
    pattern the MCP path (E7 above) must mirror."""
    echo = _Echo()
    await echo.start()
    monkeypatch.setenv("OPENAI_BASE_URL", f"http://127.0.0.1:{echo.port}")
    resolver = _RecordingResolver()
    runner, port = await _start_proxy(resolver)
    try:
        token = _token(user_id="userA", tenant_id="tenant-a")
        url = f"http://127.0.0.1:{port}/proxy/{token}/openai/v1/chat/completions"
        async with (
            aiohttp.ClientSession() as session,
            session.post(url, data=b"{}", headers={_USER_ID_HEADER: "userB"}) as resp,
        ):
            await resp.read()

        # Credential selected strictly by the token's tenant; the forged
        # header never enters the lookup key.
        assert resolver.calls == [("tenant-a", "openai")], (
            f"LLM credential must be keyed on the token's tenant, not the "
            f"forged header; resolver calls={resolver.calls}"
        )
        upstream = echo.last_headers()
        assert upstream.get("authorization") == "Bearer KEY-tenant-a"
        assert _USER_ID_HEADER.lower() not in upstream
    finally:
        await runner.cleanup()
        await echo.stop()
