"""Integration tests for ``/api/v1/mcp-servers``.

Hits the FastAPI app via httpx ASGI transport against a real
Postgres testcontainer. Covers RLS isolation, ``auth_mode`` default
behaviour, 409 on binding-referenced DELETE, and ``egress.mcp.changed``
event fan-out (one event per mutating operation).
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


async def _make_user(slug: str = "mcp") -> AuthenticatedUser:
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


def _payload(**overrides) -> dict:
    base = {
        "name": f"srv-{uuid.uuid4().hex[:6]}",
        "type": "http",
        "url": "https://mcp.example.com",
        "auth_mode": "service",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_create_then_list_then_get_round_trip() -> None:
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers",
            json=_payload(name="primary", description="desc"),
            headers={"Authorization": "Bearer x"},
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]

        listing = await ac.get(
            "/api/v1/mcp-servers", headers={"Authorization": "Bearer x"},
        )
        assert listing.status_code == 200
        names = [r["name"] for r in listing.json()]
        assert "primary" in names

        detail = await ac.get(
            f"/api/v1/mcp-servers/{cid}",
            headers={"Authorization": "Bearer x"},
        )
        assert detail.status_code == 200
        assert detail.json()["description"] == "desc"


async def test_create_uses_explicit_auth_mode() -> None:
    """``auth_mode`` reaches the row exactly as sent.

    The DB column has a ``'service'`` default and the API surface
    declares ``auth_mode`` required — but a regression that wired
    the handler to ignore the body and fall through to the default
    would silently weaken the contract. Force-checked here.
    """
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/mcp-servers",
            json=_payload(auth_mode="both"),
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 201
    assert resp.json()["auth_mode"] == "both"


async def test_create_rejects_missing_auth_mode_as_422() -> None:
    """The Pydantic enum + ``required`` set blocks an omitted ``auth_mode``."""
    user = await _make_user()
    body = _payload()
    body.pop("auth_mode")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/mcp-servers", json=body,
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 422


async def test_create_duplicate_name_returns_409() -> None:
    user = await _make_user()
    body = _payload(name="dup")
    async with _client(_build_app(user)) as ac:
        first = await ac.post(
            "/api/v1/mcp-servers", json=body,
            headers={"Authorization": "Bearer x"},
        )
        second = await ac.post(
            "/api/v1/mcp-servers", json=body,
            headers={"Authorization": "Bearer x"},
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["code"] == "RESOURCE_IN_USE"


async def test_patch_partial_leaves_unspecified_fields_alone() -> None:
    """``description`` change must not wipe ``extra_headers``."""
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers",
            json=_payload(
                name="patchable",
                extra_headers={"X-Tenant": "abc"},
                tool_reversibility={"safe_tool": True},
            ),
            headers={"Authorization": "Bearer x"},
        )
        cid = created.json()["id"]
        patched = await ac.patch(
            f"/api/v1/mcp-servers/{cid}",
            json={"description": "new"},
            headers={"Authorization": "Bearer x"},
        )
    assert patched.status_code == 200
    body = patched.json()
    assert body["description"] == "new"
    assert body["extra_headers"] == {"X-Tenant": "abc"}
    assert body["tool_reversibility"] == {"safe_tool": True}


# ---------------------------------------------------------------------------
# DELETE 409
# ---------------------------------------------------------------------------


async def test_delete_returns_409_when_bound_to_coworker() -> None:
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers",
            json=_payload(name="bound"),
            headers={"Authorization": "Bearer x"},
        )
        mcp_id = created.json()["id"]

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend) "
            "VALUES ($1::uuid, $2, $3, $4) RETURNING id",
            user.tenant_id, "marketing", f"mk-{uuid.uuid4().hex[:6]}",
            "claude",
        )
        await conn.execute(
            "INSERT INTO coworker_mcp_servers (coworker_id, mcp_server_id) "
            "VALUES ($1::uuid, $2::uuid)",
            str(cw_id), mcp_id,
        )

    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/mcp-servers/{mcp_id}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "RESOURCE_IN_USE"
    assert str(cw_id) in body["details"]["coworker_ids"]


async def test_delete_succeeds_when_no_bindings() -> None:
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers",
            json=_payload(name="lonely"),
            headers={"Authorization": "Bearer x"},
        )
        mcp_id = created.json()["id"]
        resp = await ac.delete(
            f"/api/v1/mcp-servers/{mcp_id}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 204


async def test_get_returns_404_with_envelope_for_garbage_uuid() -> None:
    """Both a bad-UUID and a not-found row must surface the same 404.

    Leaks of the DB's UUID parsing rules would tell an attacker
    whether their guess is even structurally valid; we collapse both
    to one 404.
    """
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            "/api/v1/mcp-servers/not-a-uuid",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Event fan-out
# ---------------------------------------------------------------------------


async def test_create_publishes_egress_event(monkeypatch) -> None:
    """One ``egress.mcp.changed`` event per create."""
    user = await _make_user()
    seen: list[dict] = []

    async def _capture(*, action: str, row) -> None:  # noqa: ANN001
        seen.append({"action": action, "name": row.name})

    monkeypatch.setattr(
        "webui.v1.mcp_servers.mcp_events.publish_mcp_server_changed",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        await ac.post(
            "/api/v1/mcp-servers", json=_payload(name="emitting"),
            headers={"Authorization": "Bearer x"},
        )
    assert seen == [{"action": "created", "name": "emitting"}]


async def test_patch_publishes_updated_event(monkeypatch) -> None:
    user = await _make_user()
    seen: list[dict] = []

    async def _capture(*, action: str, row) -> None:  # noqa: ANN001
        seen.append({"action": action, "name": row.name, "url": row.url})

    monkeypatch.setattr(
        "webui.v1.mcp_servers.mcp_events.publish_mcp_server_changed",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers", json=_payload(name="patch-emit"),
            headers={"Authorization": "Bearer x"},
        )
        seen.clear()
        cid = created.json()["id"]
        await ac.patch(
            f"/api/v1/mcp-servers/{cid}",
            json={"url": "https://other.example.com"},
            headers={"Authorization": "Bearer x"},
        )
    assert seen == [
        {"action": "updated", "name": "patch-emit",
         "url": "https://other.example.com"},
    ]


async def test_delete_publishes_deleted_event(monkeypatch) -> None:
    user = await _make_user()
    deleted: list[str] = []

    async def _capture(*, name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(
        "webui.v1.mcp_servers.mcp_events.publish_mcp_server_deleted",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers", json=_payload(name="to-delete"),
            headers={"Authorization": "Bearer x"},
        )
        cid = created.json()["id"]
        resp = await ac.delete(
            f"/api/v1/mcp-servers/{cid}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 204
    assert deleted == ["to-delete"]


async def test_delete_409_does_not_emit_event(monkeypatch) -> None:
    """409 path must not announce an absent change to the gateway."""
    user = await _make_user()
    deleted: list[str] = []

    async def _capture(*, name: str) -> None:
        deleted.append(name)

    monkeypatch.setattr(
        "webui.v1.mcp_servers.mcp_events.publish_mcp_server_deleted",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        created = await ac.post(
            "/api/v1/mcp-servers", json=_payload(name="bound-409"),
            headers={"Authorization": "Bearer x"},
        )
        mcp_id = created.json()["id"]
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend) "
            "VALUES ($1::uuid, $2, $3, $4) RETURNING id",
            user.tenant_id, "marketing", f"m-{uuid.uuid4().hex[:6]}", "claude",
        )
        await conn.execute(
            "INSERT INTO coworker_mcp_servers (coworker_id, mcp_server_id) "
            "VALUES ($1::uuid, $2::uuid)",
            str(cw_id), mcp_id,
        )
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/mcp-servers/{mcp_id}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 409
    assert deleted == []


# ---------------------------------------------------------------------------
# RLS isolation
# ---------------------------------------------------------------------------


async def test_mcp_servers_isolated_per_tenant() -> None:
    a = await _make_user("a")
    b = await _make_user("b")
    async with _client(_build_app(a)) as ac:
        await ac.post(
            "/api/v1/mcp-servers", json=_payload(name="a-only"),
            headers={"Authorization": "Bearer x"},
        )
    async with _client(_build_app(b)) as ac:
        listing = await ac.get(
            "/api/v1/mcp-servers", headers={"Authorization": "Bearer x"},
        )
    assert listing.status_code == 200
    assert listing.json() == []
