"""Integration tests for ``/api/v1/models`` (read-only).

Runs against a real Postgres testcontainer. The model catalog is
populated by ``_create_schema`` (idempotent seed) so the suite reads
back the seeded rows rather than constructing fresh ones.
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


async def _make_user() -> AuthenticatedUser:
    t = await create_tenant(
        name="models", slug=f"m-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner", email="x@x.com", name="X",
    )


async def test_list_models_returns_seeded_catalog() -> None:
    """The schema seeder writes a non-empty catalog; the list returns it."""
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.get("/api/v1/models", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    providers = {row["provider"] for row in body}
    # Seed always includes Anthropic; if a future cleanup ever drops it
    # we want to know.
    assert "anthropic" in providers


async def test_list_models_filters_by_provider() -> None:
    """``?provider=anthropic`` narrows the result to anthropic rows only."""
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            "/api/v1/models?provider=anthropic",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body, "seed has anthropic rows"
    assert all(row["provider"] == "anthropic" for row in body)


async def test_list_models_filters_by_family() -> None:
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            "/api/v1/models?family=claude",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body
    assert all(row["model_family"] == "claude" for row in body)


async def test_list_models_rejects_unknown_provider() -> None:
    """Pydantic enum validation rejects unrecognised providers at 422."""
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            "/api/v1/models?provider=nonesuch",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 422


async def test_get_model_by_id_round_trip() -> None:
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        listing = await ac.get(
            "/api/v1/models", headers={"Authorization": "Bearer x"},
        )
        first = listing.json()[0]
        detail = await ac.get(
            f"/api/v1/models/{first['id']}",
            headers={"Authorization": "Bearer x"},
        )
    assert detail.status_code == 200
    assert detail.json()["id"] == first["id"]


async def test_get_model_not_found_returns_envelope() -> None:
    user = await _make_user()
    bogus = str(uuid.uuid4())
    async with _client(_build_app(user)) as ac:
        resp = await ac.get(
            f"/api/v1/models/{bogus}",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"
    assert body["details"]["model_id"] == bogus
