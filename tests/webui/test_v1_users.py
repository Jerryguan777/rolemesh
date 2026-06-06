"""Integration tests for ``/api/v1/users`` — tenant user management.

Equivalence migration off the legacy ``/api/admin/users`` face. Pins:
  1. Role gate — ``user.manage`` (owner + admin); member is 403.
  2. Owner-only role escalation — only an owner may create/assign the
     ``owner`` role (in-handler ``FORBIDDEN`` envelope).
  3. Cross-tenant rows are 404 (never 403 — existence not leaked).
  4. Self-delete guard (400) and the standard CRUD round-trip.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import create_tenant, create_user, get_user
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


async def _make_tenant_user(
    role: str = "owner", tenant_id: str | None = None,
) -> AuthenticatedUser:
    if tenant_id is None:
        t = await create_tenant(name="T", slug=f"usr-{uuid.uuid4().hex[:8]}")
        tenant_id = t.id
    u = await create_user(
        tenant_id=tenant_id,
        name="A",
        email=f"x-{uuid.uuid4().hex[:6]}@x.com",
        role=role,  # type: ignore[arg-type]
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=tenant_id, role=role,  # type: ignore[arg-type]
        email="x@x.com", name="X",
    )


# --- list ------------------------------------------------------------------


async def test_list_users_owner_ok() -> None:
    user = await _make_tenant_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/users", headers=_HDRS)
    assert resp.status_code == 200, resp.text
    ids = {u["id"] for u in resp.json()["items"]}
    assert user.user_id in ids


async def test_list_users_admin_ok() -> None:
    user = await _make_tenant_user("admin")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/users", headers=_HDRS)
    assert resp.status_code == 200, resp.text


async def test_list_users_pagination_envelope_and_window() -> None:
    # Seed enough users that a small limit forces two pages; assert the
    # {items, total, limit, offset} envelope and that limit/offset slice
    # without overlap and total reflects the full set (not the page).
    owner = await _make_tenant_user("owner")
    for _ in range(4):  # owner + 4 = 5 users in the tenant
        await create_user(
            tenant_id=owner.tenant_id, name="U",
            email=f"u-{uuid.uuid4().hex[:6]}@x.com", role="member",
        )
    async with _client(_build_app(owner)) as ac:
        p1 = (await ac.get("/api/v1/users?limit=2&offset=0", headers=_HDRS)).json()
        p2 = (await ac.get("/api/v1/users?limit=2&offset=2", headers=_HDRS)).json()
    assert p1["total"] == 5
    assert p1["limit"] == 2 and p1["offset"] == 0
    assert len(p1["items"]) == 2
    assert p2["offset"] == 2 and len(p2["items"]) == 2
    # No overlap between consecutive pages.
    assert {u["id"] for u in p1["items"]}.isdisjoint(
        {u["id"] for u in p2["items"]}
    )


async def test_list_users_limit_over_max_is_rejected() -> None:
    owner = await _make_tenant_user("owner")
    async with _client(_build_app(owner)) as ac:
        resp = await ac.get("/api/v1/users?limit=999", headers=_HDRS)
    assert resp.status_code == 422  # exceeds MAX_PAGE_LIMIT (200)


async def test_list_users_member_forbidden() -> None:
    # Route-level gate (require_action) → plain 403 (no envelope code).
    user = await _make_tenant_user("member")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/users", headers=_HDRS)
    assert resp.status_code == 403


# --- create ----------------------------------------------------------------


async def test_create_user_happy_path() -> None:
    user = await _make_tenant_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/users",
            json={"name": "New", "email": "new@x.com", "role": "member"},
            headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "New"
    assert body["role"] == "member"
    assert body["tenant_id"] == user.tenant_id


async def test_admin_cannot_create_owner_role() -> None:
    # In-handler business rule → FORBIDDEN envelope (not the gate).
    user = await _make_tenant_user("admin")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/users",
            json={"name": "O", "email": "o@x.com", "role": "owner"},
            headers=_HDRS,
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_create_user_member_forbidden() -> None:
    user = await _make_tenant_user("member")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/users",
            json={"name": "x", "role": "member"},
            headers=_HDRS,
        )
    assert resp.status_code == 403


# --- get / cross-tenant ----------------------------------------------------


async def test_get_user_detail_ok() -> None:
    user = await _make_tenant_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(f"/api/v1/users/{user.user_id}", headers=_HDRS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == user.user_id


async def test_get_user_cross_tenant_is_404() -> None:
    # A user that exists in another tenant must read as 404, not 403.
    other = await _make_tenant_user("member")
    caller = await _make_tenant_user("owner")
    async with _client(_build_app(caller)) as ac:
        resp = await ac.get(f"/api/v1/users/{other.user_id}", headers=_HDRS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# --- update ----------------------------------------------------------------


async def test_update_user_name() -> None:
    owner = await _make_tenant_user("owner")
    target = await create_user(
        tenant_id=owner.tenant_id, name="Before",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com", role="member",
    )
    async with _client(_build_app(owner)) as ac:
        resp = await ac.patch(
            f"/api/v1/users/{target.id}",
            json={"name": "After"},
            headers=_HDRS,
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "After"


async def test_admin_cannot_assign_owner_role() -> None:
    admin = await _make_tenant_user("admin")
    target = await create_user(
        tenant_id=admin.tenant_id, name="M",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com", role="member",
    )
    async with _client(_build_app(admin)) as ac:
        resp = await ac.patch(
            f"/api/v1/users/{target.id}",
            json={"role": "owner"},
            headers=_HDRS,
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_update_user_cross_tenant_is_404() -> None:
    other = await _make_tenant_user("member")
    caller = await _make_tenant_user("owner")
    async with _client(_build_app(caller)) as ac:
        resp = await ac.patch(
            f"/api/v1/users/{other.user_id}",
            json={"name": "x"},
            headers=_HDRS,
        )
    assert resp.status_code == 404


# --- delete ----------------------------------------------------------------


async def test_delete_user_ok() -> None:
    owner = await _make_tenant_user("owner")
    target = await create_user(
        tenant_id=owner.tenant_id, name="Doomed",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com", role="member",
    )
    async with _client(_build_app(owner)) as ac:
        resp = await ac.delete(f"/api/v1/users/{target.id}", headers=_HDRS)
    assert resp.status_code == 204
    assert await get_user(target.id, tenant_id=owner.tenant_id) is None


async def test_delete_self_rejected() -> None:
    owner = await _make_tenant_user("owner")
    async with _client(_build_app(owner)) as ac:
        resp = await ac.delete(f"/api/v1/users/{owner.user_id}", headers=_HDRS)
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_delete_user_cross_tenant_is_404() -> None:
    other = await _make_tenant_user("member")
    caller = await _make_tenant_user("owner")
    async with _client(_build_app(caller)) as ac:
        resp = await ac.delete(f"/api/v1/users/{other.user_id}", headers=_HDRS)
    assert resp.status_code == 404
