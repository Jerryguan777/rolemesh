"""Integration tests for ``/api/v1/admin/models`` (PR24).

Platform model catalog writes. Three big behaviors to pin:
  1. Role gate — only owners may mutate the catalog
  2. Soft-delete semantics — in-use rows return 409, not destruction
  3. (provider, model_id) uniqueness
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_model,
    create_tenant,
    create_user,
    get_model_by_id,
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


async def _make_user(role: str = "owner") -> AuthenticatedUser:
    t = await create_tenant(
        name="T", slug=f"adm-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="A",
        email=f"x-{uuid.uuid4().hex[:6]}@x.com",
        role=role,  # type: ignore[arg-type]
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role=role,  # type: ignore[arg-type]
        email="x@x.com", name="X",
    )


async def test_create_model_happy_path() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/admin/models",
            json={
                "provider": "anthropic",
                "model_id": f"claude-opus-{uuid.uuid4().hex[:6]}",
                "model_family": "claude",
                "display_name": "Claude Opus Test",
            },
            headers=_HDRS,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_name"] == "Claude Opus Test"
    assert body["is_active"] is True


async def test_create_model_rejected_for_non_owner() -> None:
    # Role gate: members get 403 with a FORBIDDEN code. Pins the
    # specific code so the SPA can branch on it (e.g. hide the
    # admin UI for non-owners rather than show a generic error).
    user = await _make_user("member")
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/admin/models",
            json={
                "provider": "anthropic",
                "model_id": "x",
                "model_family": "claude",
                "display_name": "x",
            },
            headers=_HDRS,
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_create_model_duplicate_returns_409() -> None:
    user = await _make_user("owner")
    model_id_str = f"dup-{uuid.uuid4().hex[:6]}"
    async with _client(_build_app(user)) as ac:
        first = await ac.post(
            "/api/v1/admin/models",
            json={
                "provider": "anthropic",
                "model_id": model_id_str,
                "model_family": "claude",
                "display_name": "First",
            },
            headers=_HDRS,
        )
        assert first.status_code == 201
        second = await ac.post(
            "/api/v1/admin/models",
            json={
                "provider": "anthropic",
                "model_id": model_id_str,
                "model_family": "claude",
                "display_name": "Second",
            },
            headers=_HDRS,
        )
    assert second.status_code == 409


async def test_patch_model_updates_display_name_and_is_active() -> None:
    user = await _make_user("owner")
    row = await create_model(
        provider="anthropic",
        model_id=f"patch-{uuid.uuid4().hex[:6]}",
        model_family="claude",
        display_name="Before",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            f"/api/v1/admin/models/{row.id}",
            json={"display_name": "After", "is_active": False},
            headers=_HDRS,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "After"
    assert body["is_active"] is False


async def test_delete_unused_model_succeeds_with_soft_delete() -> None:
    # No coworker bindings → DELETE returns 204 and flips
    # is_active=false (not a row drop).
    user = await _make_user("owner")
    row = await create_model(
        provider="anthropic",
        model_id=f"un-{uuid.uuid4().hex[:6]}",
        model_family="claude",
        display_name="Unused",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/admin/models/{row.id}", headers=_HDRS,
        )
    assert resp.status_code == 204
    after = await get_model_by_id(row.id)
    assert after is not None, "soft delete must not drop the row"
    assert after.is_active is False


async def test_delete_in_use_model_returns_409() -> None:
    # Bind a coworker to the model first, then DELETE — 409 with
    # the bound count surfaced so the UI can show "N coworkers
    # depend on this".
    user = await _make_user("owner")
    row = await create_model(
        provider="anthropic",
        model_id=f"used-{uuid.uuid4().hex[:6]}",
        model_family="claude",
        display_name="Used",
    )
    await create_coworker(
        tenant_id=user.tenant_id,
        name="CW",
        folder=f"cw-mu-{uuid.uuid4().hex[:6]}",
        model_id=row.id,
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/admin/models/{row.id}", headers=_HDRS,
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "RESOURCE_IN_USE"
    assert body["details"]["coworker_count"] >= 1
    # Row must still be is_active=True since the 409 short-circuited
    # the soft-delete.
    after = await get_model_by_id(row.id)
    assert after is not None and after.is_active is True


async def test_delete_missing_model_returns_404() -> None:
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            f"/api/v1/admin/models/{uuid.uuid4()}", headers=_HDRS,
        )
    assert resp.status_code == 404


async def test_patch_non_owner_rejected() -> None:
    # Both PATCH and DELETE need the role gate; pin the PATCH path
    # separately so a future refactor that re-wires only POST
    # doesn't open a hole.
    user = await _make_user("member")
    row = await create_model(
        provider="anthropic",
        model_id=f"mem-{uuid.uuid4().hex[:6]}",
        model_family="claude",
        display_name="x",
    )
    async with _client(_build_app(user)) as ac:
        resp = await ac.patch(
            f"/api/v1/admin/models/{row.id}",
            json={"display_name": "should-fail"},
            headers=_HDRS,
        )
    assert resp.status_code == 403
