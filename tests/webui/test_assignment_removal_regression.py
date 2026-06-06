"""Regression guard for feat/roles PR4 — removal of user_agent_assignments.

The user-agent assignment mechanism (table + CRUD + admin endpoints + OIDC
auto-assign) was deleted. The whole point of this file is to prove that the
removal did NOT regress agent access: access is governed purely by coworker
``visibility`` + ``created_by_user_id`` ownership, never by an assignment row.

Concretely, after removal:
  * a ``member`` can create a conversation with / use a SHARED coworker
    end-to-end (this previously could have depended on an assignment row);
  * a ``member`` still CANNOT use another member's PRIVATE coworker
    (visibility still governs, nothing fell open);
  * the deleted admin assignment routes are gone (404/405);
  * the ``user_agent_assignments`` table no longer exists in the schema.

DO NOT re-introduce an assignment-style gate: if a future change makes the
shared-agent case below start failing, the fix is to restore visibility-based
access, not to add back a per-user assignment table.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import create_coworker, create_tenant, create_user
from rolemesh.db._pool import admin_conn
from webui import admin
from webui.api_v1 import router as api_v1_router
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _authed(tenant_id: str, user_id: str, role: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role=role,
        email="x@x.com", name="X",
    )


def _build_v1_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


def _build_admin_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    app.include_router(admin.router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    app.dependency_overrides[require_manage_agents] = _return_user
    app.dependency_overrides[require_manage_tenant] = _return_user
    app.dependency_overrides[require_manage_users] = _return_user
    return app


async def _tenant() -> str:
    t = await create_tenant(name="T", slug=f"asg-rm-{uuid.uuid4().hex[:8]}")
    return t.id


async def _user(tenant_id: str, role: str) -> str:
    u = await create_user(
        tenant_id=tenant_id, name="U",
        email=f"u-{uuid.uuid4().hex[:8]}@x.com", role=role,
    )
    return u.id


async def _coworker(tenant_id: str, *, created_by: str | None, visibility: str) -> str:
    cw = await create_coworker(
        tenant_id=tenant_id, name=f"CW {uuid.uuid4().hex[:6]}",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
        created_by_user_id=created_by,
        visibility=visibility,
    )
    return cw.id


# ---------------------------------------------------------------------------
# Core regression: access survives assignment removal, governed by visibility.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_can_use_shared_agent_without_any_assignment() -> None:
    """A member who was NEVER assigned the coworker can still open a
    conversation with a SHARED one. Before PR4 this path could have been
    gated by an assignment row; it must now work on visibility alone."""
    tid = await _tenant()
    creator = await _user(tid, "member")
    nobody_assigned_me = await _user(tid, "member")
    shared = await _coworker(tid, created_by=creator, visibility="shared")

    app = _build_v1_app(_authed(tid, nobody_assigned_me, "member"))
    async with _client(app) as c:
        resp = await c.post(
            f"/api/v1/coworkers/{shared}/conversations", json={"name": "hi"}
        )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_member_cannot_use_other_members_private_agent() -> None:
    """Visibility still governs after removal: a member cannot open a
    conversation with another member's PRIVATE coworker (404 — existence
    hidden). This proves access did not silently fall open when the
    assignment table was deleted."""
    tid = await _tenant()
    creator = await _user(tid, "member")
    intruder = await _user(tid, "member")
    private = await _coworker(tid, created_by=creator, visibility="private")

    app = _build_v1_app(_authed(tid, intruder, "member"))
    async with _client(app) as c:
        resp = await c.post(
            f"/api/v1/coworkers/{private}/conversations", json={"name": "hi"}
        )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# The deleted admin assignment routes must be gone.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_admin_routes_are_removed() -> None:
    """The three assignment endpoints were deleted. FastAPI returns 404 for
    an unknown path and 405 when the path exists with a different method;
    either proves the assignment route is gone (and not, e.g., accidentally
    shadowed by another handler that 200s)."""
    tid = await _tenant()
    admin_id = await _user(tid, "admin")
    member_id = await _user(tid, "member")
    cw = await _coworker(tid, created_by=admin_id, visibility="shared")

    app = _build_admin_app(_authed(tid, admin_id, "admin"))
    async with _client(app) as c:
        # POST /agents/{id}/assign  (was: assign_agent)
        r1 = await c.post(
            f"/api/admin/agents/{cw}/assign", json={"user_id": member_id}
        )
        assert r1.status_code in (404, 405), r1.text
        # DELETE /agents/{id}/assign/{user_id}  (was: unassign_agent)
        r2 = await c.delete(f"/api/admin/agents/{cw}/assign/{member_id}")
        assert r2.status_code in (404, 405), r2.text
        # GET /agents/{id}/users  (was: list_assigned_users)
        r3 = await c.get(f"/api/admin/agents/{cw}/users")
        assert r3.status_code in (404, 405), r3.text


@pytest.mark.asyncio
async def test_user_detail_has_no_assigned_agents_field() -> None:
    """GET /users/{id} was surgically edited (not deleted). It must still
    return the user, but the ``assigned_agents`` field is gone from the
    response contract."""
    tid = await _tenant()
    admin_id = await _user(tid, "admin")
    target = await _user(tid, "member")

    app = _build_admin_app(_authed(tid, admin_id, "admin"))
    async with _client(app) as c:
        resp = await c.get(f"/api/admin/users/{target}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == target
    assert "assigned_agents" not in body, body


# ---------------------------------------------------------------------------
# The table itself must be gone from the schema (idempotent DROP applied).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_agent_assignments_table_does_not_exist() -> None:
    """After schema init, the table must not exist — neither the CREATE in
    schema.py nor the test-reset DROP should leave it behind."""
    async with admin_conn() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass('public.user_agent_assignments') IS NOT NULL"
        )
    assert exists is False
