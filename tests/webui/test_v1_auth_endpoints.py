"""``/api/v1/auth/*`` and ``/api/v1/me`` endpoint coverage.

01b's WS handshake depends on the ticket shape this issuer produces;
the verifier sees only what's in the JWT payload. So the bug
surface is at issuance: a missing claim, a too-long exp, a leaky
``conversation_id`` not actually owned by the caller. These tests
focus on those seams rather than mirroring the JWT library's own
unit tests.
"""

from __future__ import annotations

import os
import uuid

import httpx
import jwt
import pytest
from fastapi import FastAPI

from rolemesh.auth.bootstrap_users import (
    BootstrapUserSpec,
    _reset_for_tests,
    ensure_bootstrap_user_row,
    init_bootstrap_users,
)
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    _get_admin_pool,
    create_tenant,
    create_user,
)
from webui import auth as webui_auth
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


# Ensure a known secret across this whole test module so JWT decode
# in the assertions matches what the issuer signed. Set before any
# test imports ws_ticket so the module-level cache (if any) sees it.
_TEST_SECRET = "v1-ws-ticket-secret-only-for-tests"
os.environ.setdefault("WS_TICKET_SECRET", _TEST_SECRET)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _build_app(user: AuthenticatedUser | None) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        assert user is not None
        return user

    if user is not None:
        app.dependency_overrides[get_current_user] = _return_user
    return app


def _authed(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        tenant_id=tenant_id,
        role="owner",
        email="alice@x.com",
        name="Alice",
    )


async def _seed_conv(tenant_id: str) -> str:
    pool = _get_admin_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            tenant_id,
        )
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend) "
            "VALUES ($1::uuid, $2, $3, 'claude') RETURNING id::text",
            tenant_id, f"cw-{uuid.uuid4().hex[:6]}", f"f-{uuid.uuid4().hex[:6]}",
        )
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings "
            "(tenant_id, coworker_id, channel_type, credentials) "
            "VALUES ($1::uuid, $2::uuid, 'web', '{}'::jsonb) "
            "RETURNING id::text",
            tenant_id, cw_id,
        )
        conv_id = await conn.fetchval(
            "INSERT INTO conversations "
            "(tenant_id, coworker_id, channel_binding_id, channel_chat_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4) "
            "RETURNING id::text",
            tenant_id, cw_id, binding_id, f"chat-{uuid.uuid4().hex[:6]}",
        )
    return conv_id


async def _make_tenant_and_user(slug_prefix: str = "v1auth") -> tuple[str, str]:
    t = await create_tenant(
        name=f"T-{slug_prefix}",
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return t.id, u.id


# ---------------------------------------------------------------------------
# /auth/config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_config_reports_bootstrap_when_bootstrap_users_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "BOOTSTRAP_USERS",
        '[{"token":"tok-a","user_id":"a","tenant":"default","role":"owner"}]',
    )
    monkeypatch.delenv("AUTH_MODE", raising=False)
    app = _build_app(None)
    async with _client(app) as c:
        resp = await c.get("/api/v1/auth/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"mode": "bootstrap", "login_url": None}


@pytest.mark.asyncio
async def test_auth_config_reports_oidc_with_login_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BOOTSTRAP_USERS", raising=False)
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://idp.example/redirect")
    app = _build_app(None)
    async with _client(app) as c:
        resp = await c.get("/api/v1/auth/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "oidc"
    assert body["login_url"] == "https://idp.example/redirect"


@pytest.mark.asyncio
async def test_auth_config_does_not_leak_token_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token string should ever appear in /auth/config response.

    The endpoint is public; leaking a token here defeats every other
    layer.
    """
    monkeypatch.setenv(
        "BOOTSTRAP_USERS",
        '[{"token":"very-secret-token-value","user_id":"a",'
        '"tenant":"default","role":"owner"}]',
    )
    app = _build_app(None)
    async with _client(app) as c:
        resp = await c.get("/api/v1/auth/config")
    text = resp.text
    assert "very-secret-token-value" not in text


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_returns_caller_identity() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.get("/api/v1/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == uid
    assert body["tenant_id"] == tid
    assert body["role"] == "owner"
    assert body["name"] == "Alice"
    # Capabilities + plane are surfaced from the role->action matrix so the
    # SPA renders affordances without copying the matrix client-side.
    assert body["plane"] == "tenant"
    assert "coworker.manage" in body["capabilities"]
    assert "credential.pool.manage" not in body["capabilities"]  # platform-only


# ---------------------------------------------------------------------------
# /auth/ws-ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_ticket_round_trip_carries_required_claims() -> None:
    tid, uid = await _make_tenant_and_user()
    conv_id = await _seed_conv(tid)
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket",
            json={"conversation_id": conv_id},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_in_s"] == 60
    decoded = jwt.decode(
        body["ticket"],
        _TEST_SECRET,
        algorithms=["HS256"],
        audience="rolemesh-ws",
    )
    assert decoded["sub"] == uid
    assert decoded["tenant_id"] == tid
    assert decoded["conversation_id"] == conv_id
    # exp must be in the future and ≤ 60s ahead.
    import time
    ttl = decoded["exp"] - int(time.time())
    assert 0 < ttl <= 60


@pytest.mark.asyncio
async def test_ws_ticket_rejects_cross_tenant_conversation() -> None:
    """A ticket request for another tenant's conversation 404s.

    404 not 403: the existence of the conversation must not leak.
    The bug bait if the check is dropped is "any authenticated
    caller can mint a ticket for any UUID they brute-force"; this
    test catches it because tenant B issues against tenant A's
    conv_id.
    """
    tid_a, _ = await _make_tenant_and_user("auth-a")
    tid_b, uid_b = await _make_tenant_and_user("auth-b")
    conv_a = await _seed_conv(tid_a)
    app = _build_app(_authed(tid_b, uid_b))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket",
            json={"conversation_id": conv_a},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_ws_ticket_requires_conversation_id() -> None:
    """Empty body / missing field -> 422 Pydantic validation error."""
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post("/api/v1/auth/ws-ticket", json={})
    assert resp.status_code == 422  # Pydantic, not v1 envelope


@pytest.mark.asyncio
async def test_ws_ticket_rejects_bad_uuid_with_404() -> None:
    """A non-UUID conversation_id collapses to NOT_FOUND, not 500."""
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket",
            json={"conversation_id": "not-a-uuid"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ws_ticket_signed_with_dedicated_secret() -> None:
    """Tickets are signed with WS_TICKET_SECRET and verify under it
    alone — an unrelated key must not validate the signature.

    Pins that the ticket scheme uses its own dedicated key (there is
    no fallback to any other secret).
    """
    tid, uid = await _make_tenant_and_user()
    conv_id = await _seed_conv(tid)
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket",
            json={"conversation_id": conv_id},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Verifies under the dedicated secret.
    jwt.decode(
        body["ticket"], _TEST_SECRET, algorithms=["HS256"], audience="rolemesh-ws"
    )
    # ...but not under some unrelated key.
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            body["ticket"],
            "some-unrelated-key",
            algorithms=["HS256"],
            audience="rolemesh-ws",
        )


# ---------------------------------------------------------------------------
# Bootstrap multi-user end-to-end (alice/bob real UUIDs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_multi_user_ticket_carries_real_alice_uuid() -> None:
    """Bootstrap spec for alice -> ticket sub is alice's stable UUID.

    The multi-user spec promotes alice/bob to real UUID rows via
    ``ensure_bootstrap_user_row``. The ticket must carry that real
    UUID so the WS handshake can attribute traffic to a real
    principal.
    """
    tid, _ = await _make_tenant_and_user("auth-mu")
    conv_id = await _seed_conv(tid)

    # Mint a spec mid-test (init_bootstrap_users normally runs at
    # process boot) and upsert alice via the same helper auth uses.
    _reset_for_tests()
    init_bootstrap_users(
        env_value='[{"token":"tok-alice","user_id":"alice","tenant":"default","role":"owner"}]'
    )
    spec = BootstrapUserSpec(
        token="tok-alice", user_id_slug="alice",
        tenant_slug="default", role="owner",
    )
    alice_uuid = await ensure_bootstrap_user_row(spec, tid)

    alice_user = AuthenticatedUser(
        user_id=alice_uuid, tenant_id=tid, role="owner",
        email=None, name="alice",
    )
    app = _build_app(alice_user)
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/auth/ws-ticket",
            json={"conversation_id": conv_id},
        )
    assert resp.status_code == 200, resp.text
    decoded = jwt.decode(
        resp.json()["ticket"],
        _TEST_SECRET,
        algorithms=["HS256"],
        audience="rolemesh-ws",
    )
    assert decoded["sub"] == alice_uuid
    assert len(decoded["sub"]) == 36  # real UUID, not the literal "bootstrap"

    # Cleanup so the in-memory map doesn't leak to neighbouring tests.
    _reset_for_tests()
    # Also reset webui.auth provider cache to avoid touching unrelated tests.
    _ = webui_auth  # imported to keep ruff happy
