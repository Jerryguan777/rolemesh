"""Integration tests for ``/api/v1/tenant`` — tenant settings (owner only).

Equivalence migration off the legacy ``/api/admin/tenant`` face. Pins:
  1. Owner-only gate — ``tenant.manage`` is granted to owner alone; admin
     and member are 403 on both GET and PATCH.
  2. GET/PATCH round-trip with the same response shape as the old face.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import create_tenant, create_user
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")

_HDRS = {"Authorization": "Bearer x"}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


async def _make_user(role: str) -> AuthenticatedUser:
    t = await create_tenant(name="T", slug=f"tn-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="A",
        email=f"x-{uuid.uuid4().hex[:6]}@x.com",
        role=role,  # type: ignore[arg-type]
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role=role,  # type: ignore[arg-type]
        email="x@x.com", name="X",
    )


async def test_get_tenant_owner_ok() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/tenant", headers=_HDRS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == user.tenant_id
    assert "max_concurrent_containers" in body


async def test_get_tenant_admin_forbidden() -> None:
    user = await _make_user("admin")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/tenant", headers=_HDRS)
    assert resp.status_code == 403


async def test_get_tenant_member_forbidden() -> None:
    user = await _make_user("member")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/tenant", headers=_HDRS)
    assert resp.status_code == 403


async def test_patch_tenant_owner_ok() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            "/api/v1/tenant",
            json={"name": "Renamed", "max_concurrent_containers": 7},
            headers=_HDRS,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["max_concurrent_containers"] == 7


async def test_patch_tenant_admin_forbidden() -> None:
    user = await _make_user("admin")
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            "/api/v1/tenant",
            json={"name": "nope"},
            headers=_HDRS,
        )
    assert resp.status_code == 403
