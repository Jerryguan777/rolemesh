"""Integration tests for ``/api/v1/coworkers/{id}/mcp-servers``.

Pins the tri-state ``enabled_tools`` semantics (NULL / [] / whitelist)
and the ``web.coworker.mcp_changed`` event fan-out on every
mutation.

The test focus is on the tri-state because it is the only column on
this junction — a regression there is the most likely shape this
relation can break. We exercise each state by writing through the
HTTP layer and reading back via ``GET`` so the ``enabled_tools=None``
case cannot silently collapse to ``[]`` (the most common bug shape
in PG TEXT[] handling).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    _get_admin_pool,
    create_tenant,
    create_user,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


async def _make_user(slug: str = "rel") -> AuthenticatedUser:
    t = await create_tenant(
        name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner", email="x@x.com", name="X",
    )


async def _make_coworker(tenant_id: str) -> str:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend) "
            "VALUES ($1::uuid, $2, $3, $4) RETURNING id",
            tenant_id, "cw", f"cw-{uuid.uuid4().hex[:6]}", "claude",
        )
    return str(cw_id)


async def _make_mcp(client: httpx.AsyncClient, *, name: str) -> str:
    resp = await client.post(
        "/api/v1/mcp-servers",
        json={
            "name": name,
            "type": "http",
            "url": "https://mcp.example.com",
            "auth_mode": "service",
        },
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Tri-state semantics
# ---------------------------------------------------------------------------


async def test_bind_with_omitted_enabled_tools_means_all_enabled() -> None:
    """Body without ``enabled_tools`` -> NULL in the DB -> JSON null on GET."""
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-omit")
        resp = await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["enabled_tools"] is None

    # And the GET path round-trips the NULL faithfully (no coercion
    # to []).
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT enabled_tools FROM coworker_mcp_servers "
            "WHERE coworker_id = $1::uuid AND mcp_server_id = $2::uuid",
            cw_id, mcp_id,
        )
    assert row["enabled_tools"] is None


async def test_bind_with_empty_enabled_tools_means_all_disabled() -> None:
    """``[]`` is a distinct state from ``null``."""
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-empty")
        resp = await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id, "enabled_tools": []},
            headers={"Authorization": "Bearer x"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["enabled_tools"] == []

        listing = await ac.get(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            headers={"Authorization": "Bearer x"},
        )
    assert listing.status_code == 200
    [row] = listing.json()
    assert row["enabled_tools"] == []


async def test_bind_with_whitelist_preserves_order_and_membership() -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-whitelist")
        resp = await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={
                "mcp_server_id": mcp_id,
                "enabled_tools": ["read_file", "search"],
            },
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 201
    assert resp.json()["enabled_tools"] == ["read_file", "search"]


# ---------------------------------------------------------------------------
# PATCH tri-state transitions — the four interesting moves
# ---------------------------------------------------------------------------


async def test_patch_can_transition_through_each_tri_state() -> None:
    """null -> [] -> whitelist -> null all reach DB intact.

    Each transition is the moment a "collapse None to [] on PATCH"
    regression would silently break a real operator intent: a switch
    from "all enabled" to "all disabled" must not look the same as
    "leave alone". The HTTP layer's ``model_fields_set`` check is
    the protective barrier — pin it with a multi-step PATCH chain.
    """
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-flux")
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )

        # null -> []
        r = await ac.patch(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            json={"enabled_tools": []},
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["enabled_tools"] == []

        # [] -> whitelist
        r = await ac.patch(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            json={"enabled_tools": ["tool_a"]},
            headers={"Authorization": "Bearer x"},
        )
        assert r.json()["enabled_tools"] == ["tool_a"]

        # whitelist -> null (back to all enabled)
        r = await ac.patch(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            json={"enabled_tools": None},
            headers={"Authorization": "Bearer x"},
        )
        assert r.json()["enabled_tools"] is None

        # Empty body must be a no-op — it does NOT clear the state.
        r = await ac.patch(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            json={},
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 200
        assert r.json()["enabled_tools"] is None


# ---------------------------------------------------------------------------
# DELETE / NOT_FOUND
# ---------------------------------------------------------------------------


async def test_unbind_removes_row_and_returns_204() -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-unbind")
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
        resp = await ac.delete(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 204
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM coworker_mcp_servers "
            "WHERE coworker_id = $1::uuid",
            cw_id,
        )
    assert n == 0


async def test_unbind_missing_returns_404() -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    bogus = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{bogus}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_patch_missing_binding_returns_404() -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    bogus = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{bogus}",
            json={"enabled_tools": ["x"]},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 404


async def test_bind_with_unknown_mcp_server_returns_404() -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    bogus = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": bogus},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 404


async def test_bind_duplicate_returns_409() -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-dup")
        first = await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
        second = await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "RESOURCE_IN_USE"


# ---------------------------------------------------------------------------
# Event fan-out
# ---------------------------------------------------------------------------


async def test_bind_publishes_mcp_changed(monkeypatch) -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    seen: list[dict] = []

    async def _capture(*, coworker_id: str, tenant_id: str) -> None:
        seen.append({"coworker_id": coworker_id, "tenant_id": tenant_id})

    monkeypatch.setattr(
        "webui.v1.coworker_mcp.coworker_events.publish_coworker_mcp_changed",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-emit-bind")
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
    assert seen == [{"coworker_id": cw_id, "tenant_id": user.tenant_id}]


async def test_patch_publishes_mcp_changed(monkeypatch) -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    seen: list[str] = []

    async def _capture(*, coworker_id: str, tenant_id: str) -> None:
        seen.append(coworker_id)

    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-emit-patch")
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
        monkeypatch.setattr(
            "webui.v1.coworker_mcp.coworker_events.publish_coworker_mcp_changed",
            _capture,
        )
        await ac.patch(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            json={"enabled_tools": ["t1"]},
            headers={"Authorization": "Bearer x"},
        )
    assert seen == [cw_id]


async def test_unbind_publishes_mcp_changed(monkeypatch) -> None:
    user = await _make_user()
    cw_id = await _make_coworker(user.tenant_id)
    seen: list[str] = []

    async def _capture(*, coworker_id: str, tenant_id: str) -> None:
        seen.append(coworker_id)

    async with _client(_build_app(user)) as ac:
        mcp_id = await _make_mcp(ac, name="srv-emit-unbind")
        await ac.post(
            f"/api/v1/coworkers/{cw_id}/mcp-servers",
            json={"mcp_server_id": mcp_id},
            headers={"Authorization": "Bearer x"},
        )
        monkeypatch.setattr(
            "webui.v1.coworker_mcp.coworker_events.publish_coworker_mcp_changed",
            _capture,
        )
        resp = await ac.delete(
            f"/api/v1/coworkers/{cw_id}/mcp-servers/{mcp_id}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 204
    assert seen == [cw_id]
