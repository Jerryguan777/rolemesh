"""Integration tests for ``/api/v1/coworkers/{id}/bindings`` (PR24).

Channel binding REST migrated from /api/admin/agents/{id}/bindings.
Covers the 5-op CRUD plus cross-coworker / cross-tenant isolation
which the legacy admin handler had less coverage of.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_tenant,
    create_user,
)
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


async def _make_user_and_coworker(
    slug: str = "bnd",
) -> tuple[AuthenticatedUser, str]:
    t = await create_tenant(
        name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{slug}-{uuid.uuid4().hex[:6]}",
    )
    return (
        AuthenticatedUser(
            user_id=u.id, tenant_id=t.id, role="owner",
            email="x@x.com", name="X",
        ),
        cw.id,
    )


async def test_create_then_list_round_trip() -> None:
    user, cw = await _make_user_and_coworker("rt")
    async with _client(_build_app(user)) as ac:
        post = await ac.post(
            f"/api/v1/coworkers/{cw}/bindings",
            json={
                "channel_type": "web",
                "credentials": {},
                "bot_display_name": "MyBot",
            },
            headers=_HDRS,
        )
        assert post.status_code == 201, post.text
        listing = await ac.get(
            f"/api/v1/coworkers/{cw}/bindings", headers=_HDRS,
        )
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["channel_type"] == "web"
    assert rows[0]["bot_display_name"] == "MyBot"
    # SECURITY: response shape MUST NOT carry credentials. Pin this
    # so a future refactor that lifts the field onto the response
    # type immediately fails — the whole point of write-only
    # credentials is that they never reflect back.
    assert "credentials" not in rows[0]


async def test_create_duplicate_channel_type_returns_409() -> None:
    user, cw = await _make_user_and_coworker("dup")
    async with _client(_build_app(user)) as ac:
        first = await ac.post(
            f"/api/v1/coworkers/{cw}/bindings",
            json={"channel_type": "web", "credentials": {}},
            headers=_HDRS,
        )
        assert first.status_code == 201
        second = await ac.post(
            f"/api/v1/coworkers/{cw}/bindings",
            json={"channel_type": "web", "credentials": {}},
            headers=_HDRS,
        )
    assert second.status_code == 409
    assert second.json()["code"] == "RESOURCE_IN_USE"


async def test_patch_credentials_replaces_not_merges() -> None:
    # Sending a new credentials map must replace the old one entirely.
    # A merge-by-key approach could silently keep a stale token alive
    # under a forgotten field name.
    user, cw = await _make_user_and_coworker("pcr")
    async with _client(_build_app(user)) as ac:
        post = await ac.post(
            f"/api/v1/coworkers/{cw}/bindings",
            json={
                "channel_type": "slack",
                "credentials": {"bot_token": "v1", "app_token": "v1"},
            },
            headers=_HDRS,
        )
        bid = post.json()["id"]
        patch = await ac.patch(
            f"/api/v1/coworkers/{cw}/bindings/{bid}",
            json={"credentials": {"bot_token": "v2"}},
            headers=_HDRS,
        )
    assert patch.status_code == 200
    # Wire response doesn't return credentials; reach into DB to
    # confirm the replace happened.
    from rolemesh.db import get_channel_binding

    binding = await get_channel_binding(bid, tenant_id=user.tenant_id)
    assert binding is not None
    assert binding.credentials == {"bot_token": "v2"}
    assert "app_token" not in binding.credentials


async def test_get_binding_cross_coworker_returns_404() -> None:
    # A binding under coworker A must 404 when fetched under
    # coworker B's path — protects against leaks via URL guessing.
    user, cw_a = await _make_user_and_coworker("cca")
    cw_b = await create_coworker(
        tenant_id=user.tenant_id,
        name="CWb",
        folder=f"cw-ccb-{uuid.uuid4().hex[:6]}",
    )
    async with _client(_build_app(user)) as ac:
        post = await ac.post(
            f"/api/v1/coworkers/{cw_a}/bindings",
            json={"channel_type": "web", "credentials": {}},
            headers=_HDRS,
        )
        bid = post.json()["id"]
        cross = await ac.get(
            f"/api/v1/coworkers/{cw_b.id}/bindings/{bid}", headers=_HDRS,
        )
    assert cross.status_code == 404


async def test_delete_then_get_returns_404() -> None:
    user, cw = await _make_user_and_coworker("del")
    async with _client(_build_app(user)) as ac:
        post = await ac.post(
            f"/api/v1/coworkers/{cw}/bindings",
            json={"channel_type": "web", "credentials": {}},
            headers=_HDRS,
        )
        bid = post.json()["id"]
        rm = await ac.delete(
            f"/api/v1/coworkers/{cw}/bindings/{bid}", headers=_HDRS,
        )
        assert rm.status_code == 204
        again = await ac.get(
            f"/api/v1/coworkers/{cw}/bindings/{bid}", headers=_HDRS,
        )
    assert again.status_code == 404


async def test_cross_tenant_list_returns_404_for_coworker() -> None:
    # Tenant A asking about tenant B's coworker bindings must 404
    # (the coworker isn't in their tenant).
    user_a, _ = await _make_user_and_coworker("ta")
    user_b, cw_b = await _make_user_and_coworker("tb")
    async with _client(_build_app(user_a)) as ac:
        resp = await ac.get(
            f"/api/v1/coworkers/{cw_b}/bindings", headers=_HDRS,
        )
    assert resp.status_code == 404
