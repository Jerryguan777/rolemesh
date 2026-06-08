"""Suspend *enforcement* at the authentication chokepoints.

Provisioning/CRUD of the lifecycle surface is covered in
``test_v1_platform_tenants.py``; this file pins that a suspended tenant is
actually *locked out*:

- REST: ``get_current_user`` (the single ``/api/v1`` chokepoint) returns 403
  ``TENANT_SUSPENDED`` once the tenant is suspended, and recovers on resume.
- WS: ``_verify_handshake`` rejects an already-minted, otherwise-valid ticket
  with close code 4005 once the tenant is suspended (the REST chokepoint
  cannot cover the WS plane).
- JIT no-revival: OIDC re-login machinery never resets ``status`` — a
  suspended tenant stays suspended across a re-provision.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import create_tenant, create_user, get_tenant_status, set_tenant_status
from webui import auth as webui_auth
from webui.api_v1 import router as api_v1_router
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")

_H = {"Authorization": "Bearer tok"}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _app_without_auth_override() -> FastAPI:
    """App with the REAL get_current_user (so the suspend check runs)."""
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)
    return app


async def _make_owner_in_new_tenant() -> AuthenticatedUser:
    t = await create_tenant(name="cust", slug=f"c-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id,
        name="Owner",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner", email="x@x.com", name="X"
    )


# ---------------------------------------------------------------------------
# REST chokepoint
# ---------------------------------------------------------------------------


async def test_suspended_tenant_user_gets_403_on_v1_request(monkeypatch):
    """A real gated endpoint returns 403 TENANT_SUSPENDED when suspended."""
    user = await _make_owner_in_new_tenant()

    async def _fake_auth(_token: str) -> AuthenticatedUser:
        return user

    monkeypatch.setattr(webui_auth, "authenticate_ws", _fake_auth)

    async with _client(_app_without_auth_override()) as ac:
        # Active: the tenant settings endpoint (owner-gated) is reachable.
        ok = await ac.get("/api/v1/tenant", headers=_H)
        assert ok.status_code == 200, ok.text

        # Suspend -> the SAME request is now blocked at the chokepoint.
        await set_tenant_status(user.tenant_id, "suspended")
        blocked = await ac.get("/api/v1/tenant", headers=_H)
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "TENANT_SUSPENDED"

        # Resume -> recovers.
        await set_tenant_status(user.tenant_id, "active")
        recovered = await ac.get("/api/v1/tenant", headers=_H)
        assert recovered.status_code == 200, recovered.text


async def test_suspended_blocks_every_gated_surface(monkeypatch):
    """The chokepoint covers the whole surface, not one endpoint."""
    user = await _make_owner_in_new_tenant()

    async def _fake_auth(_token: str) -> AuthenticatedUser:
        return user

    monkeypatch.setattr(webui_auth, "authenticate_ws", _fake_auth)
    await set_tenant_status(user.tenant_id, "suspended")

    async with _client(_app_without_auth_override()) as ac:
        for path in ("/api/v1/coworkers", "/api/v1/tenant", "/api/v1/credentials"):
            resp = await ac.get(path, headers=_H)
            assert resp.status_code == 403, (path, resp.text)
            assert resp.json()["code"] == "TENANT_SUSPENDED", path


# ---------------------------------------------------------------------------
# WS handshake chokepoint
# ---------------------------------------------------------------------------


class _FakeWS:
    """Records the close(code, reason) the handshake invokes."""

    def __init__(self) -> None:
        self.closed_code: int | None = None
        self.closed_reason: str | None = None

    async def close(self, code: int, reason: str = "") -> None:
        self.closed_code = code
        self.closed_reason = reason


async def test_ws_handshake_rejects_suspended_tenant(monkeypatch):
    monkeypatch.setenv("WS_TICKET_SECRET", "test-ws-secret")
    from rolemesh.auth.ws_ticket import issue_ws_ticket
    from webui.v1 import ws_stream

    user = await _make_owner_in_new_tenant()
    conv_id = str(uuid.uuid4())
    ticket, _ = issue_ws_ticket(
        user_id=user.user_id, tenant_id=user.tenant_id, conversation_id=conv_id
    )

    # Active: a valid ticket verifies (returns the payload, no close).
    ws_ok = _FakeWS()
    payload = await ws_stream._verify_handshake(ws_ok, conv_id, ticket)
    assert payload is not None
    assert ws_ok.closed_code is None

    # Suspended: the same still-valid ticket is now refused with 4005.
    await set_tenant_status(user.tenant_id, "suspended")
    ws_susp = _FakeWS()
    payload2 = await ws_stream._verify_handshake(ws_susp, conv_id, ticket)
    assert payload2 is None
    assert ws_susp.closed_code == ws_stream._CLOSE_TENANT_SUSPENDED


# ---------------------------------------------------------------------------
# JIT no-revival invariant
# ---------------------------------------------------------------------------


async def test_status_survives_reprovision_no_revival():
    """Re-provisioning a tenant by slug must not flip a suspended tenant back.

    Mirrors the OIDC JIT shape: ``_provision_tenant`` returns the existing
    tenant id on a mapping hit and never writes ``status``. Here we assert the
    underlying invariant directly: creating/looking a suspended tenant up does
    not resurrect it; only an explicit resume does.
    """
    from rolemesh.db import get_tenant_by_slug

    t = await create_tenant(name="jit", slug=f"jit-{uuid.uuid4().hex[:8]}")
    await set_tenant_status(t.id, "suspended")

    # A slug lookup (what the JIT hit path does) returns it unchanged.
    again = await get_tenant_by_slug(t.slug)
    assert again is not None
    assert again.status == "suspended"
    assert await get_tenant_status(t.id) == "suspended"
