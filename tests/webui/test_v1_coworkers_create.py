"""Integration tests for ``POST /api/v1/coworkers``.

Hits the FastAPI app via httpx ASGI transport against a real
Postgres testcontainer (no DB mock). Every test seeds its own
tenants / users / models / credentials so cross-test interference
is impossible — design §10 makes integration the default.

Coverage focuses on the validation chain — the bug-bait is in the
order and combination of failures the chain has to keep straight,
not in the happy path. The yaml says "validate combo + check
credential"; this file asserts both fire in the right combination.
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


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    """Build a transient FastAPI app pinned to ``user``.

    The dependency override matches what ``webui.dependencies``
    actually wires; the production path runs the same handler via
    Bearer-token auth on ``Authorization`` header.
    """

    app = FastAPI()
    install_error_handler(app)
    app.include_router(api_v1_router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    return app


async def _seed_models() -> dict[str, str]:
    """Insert a couple of platform models. Returns id-by-key map.

    Schema seeds usually populate these via ``schema.py`` but on a
    fresh testcontainer we control them explicitly here so the test
    intent doesn't depend on seed drift.
    """
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO models (provider, model_id, model_family, display_name) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (provider, model_id) DO NOTHING",
            "anthropic",
            "claude-opus-4-7",
            "claude",
            "Claude Opus 4.7",
        )
        await conn.execute(
            "INSERT INTO models (provider, model_id, model_family, display_name) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (provider, model_id) DO NOTHING",
            "openai",
            "gpt-4o",
            "gpt",
            "GPT-4o",
        )
        rows = await conn.fetch("SELECT id, provider, model_id FROM models")
    return {r["model_id"]: str(r["id"]) for r in rows}


async def _add_credential(tenant_id: str, provider: str) -> None:
    """Seed a credential row for the validation chain.

    The actual ciphertext shape doesn't matter to this suite — the
    chain only checks for row existence — so we insert a fixed BYTEA
    sentinel rather than reach for the real ``CredentialVault``.
    """
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_model_credentials (tenant_id, provider, credential_data) "
            "VALUES ($1::uuid, $2, $3) ON CONFLICT (tenant_id, provider) DO NOTHING",
            tenant_id,
            provider,
            b"placeholder-ciphertext",
        )


async def _make_tenant_and_user(slug_prefix: str = "v1cw") -> tuple[str, str]:
    t = await create_tenant(
        name=f"T-{slug_prefix}",
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id,
        name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    return t.id, u.id


def _authed(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role="owner",
        email="x@x.com", name="X",
    )


def _folder(prefix: str = "cw") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_coworker_minimal_no_model() -> None:
    """Creating a coworker without ``model_id`` skips the chain entirely."""
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    folder = _folder()
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "No-model coworker",
                "folder": folder,
                "agent_backend": "claude",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["folder"] == folder
    assert body["model_id"] is None
    assert body["status"] == "active"
    assert body["max_concurrent_containers"] == 2
    assert body["tenant_id"] == tid


@pytest.mark.asyncio
async def test_create_coworker_with_model_and_credential() -> None:
    """Full chain: model exists + credential exists + combo OK."""
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    await _add_credential(tid, "anthropic")
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "Helper",
                "folder": _folder(),
                "agent_backend": "claude",
                "model_id": models["claude-opus-4-7"],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["model_id"] == models["claude-opus-4-7"]


# ---------------------------------------------------------------------------
# Validation chain failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credential_returns_422_with_code() -> None:
    """No tenant credential for the model's provider -> 422 MISSING_CREDENTIAL."""
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    # Deliberately skip the credential
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "No cred",
                "folder": _folder(),
                "agent_backend": "claude",
                "model_id": models["claude-opus-4-7"],
            },
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "MISSING_CREDENTIAL"
    assert body["details"]["provider"] == "anthropic"
    assert "details" in body and "model_id" in body["details"]


@pytest.mark.asyncio
async def test_incompatible_backend_model_combo_returns_422() -> None:
    """Claude backend rejects gpt family — BACKEND_INCOMPAT."""
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    await _add_credential(tid, "openai")  # credential is fine
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "bad combo",
                "folder": _folder(),
                "agent_backend": "claude",
                "model_id": models["gpt-4o"],
            },
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "BACKEND_INCOMPAT"
    assert body["details"]["agent_backend"] == "claude"
    assert body["details"]["provider"] == "openai"


@pytest.mark.asyncio
async def test_credential_check_runs_before_combo_check() -> None:
    """When both checks would fail, MISSING_CREDENTIAL surfaces first.

    Important to pin: clients can act on a credential error
    (it tells the user what to configure); a combo error often means
    the user picked the wrong backend. Surfacing combo first would
    waste a click for the more common config-missing case.
    """
    tid, uid = await _make_tenant_and_user()
    models = await _seed_models()
    # No credential and combo mismatch -> credential takes priority
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "both wrong",
                "folder": _folder(),
                "agent_backend": "claude",
                "model_id": models["gpt-4o"],
            },
        )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "MISSING_CREDENTIAL"


@pytest.mark.asyncio
async def test_nonexistent_model_id_returns_422_model_not_found() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    ghost_uuid = str(uuid.uuid4())
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "ghost",
                "folder": _folder(),
                "agent_backend": "claude",
                "model_id": ghost_uuid,
            },
        )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "MODEL_NOT_FOUND"
    assert body["details"]["model_id"] == ghost_uuid


@pytest.mark.asyncio
async def test_duplicate_folder_in_tenant_returns_409() -> None:
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    folder = _folder()
    async with _client(app) as c:
        ok = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "first",
                "folder": folder,
                "agent_backend": "claude",
            },
        )
        assert ok.status_code == 201, ok.text
        # Use a different name on purpose — the folder constraint is
        # what should bite, not "name already exists".
        dup = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "different-name",
                "folder": folder,
                "agent_backend": "claude",
            },
        )
    assert dup.status_code == 409, dup.text
    body = dup.json()
    assert body["code"] == "RESOURCE_IN_USE"


@pytest.mark.asyncio
async def test_invalid_folder_pattern_returns_422_from_pydantic() -> None:
    """Folder regex is enforced both client- and server-side (mounted path safety)."""
    tid, uid = await _make_tenant_and_user()
    app = _build_app(_authed(tid, uid))
    async with _client(app) as c:
        resp = await c.post(
            "/api/v1/coworkers",
            json={
                "name": "bad folder",
                "folder": "../escape",
                "agent_backend": "claude",
            },
        )
    # Pydantic returns 422 by default; FastAPI's RequestValidationError
    # uses its own envelope — we only assert the rejection, not the
    # shape, because Pydantic's error path is not in our v1 envelope.
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# RLS isolation (INV-1 end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_coworkers_does_not_leak_across_tenants() -> None:
    """Tenant A creates a coworker; tenant B's GET must not see it.

    This is the INV-1 belt-and-braces case exercised end-to-end —
    handler + RLS + WHERE predicate together. A regression in any
    layer (RLS off, ``tenant_id`` dropped from WHERE) makes this red.
    """
    tid_a, uid_a = await _make_tenant_and_user("tenA")
    tid_b, uid_b = await _make_tenant_and_user("tenB")

    app_a = _build_app(_authed(tid_a, uid_a))
    app_b = _build_app(_authed(tid_b, uid_b))

    async with _client(app_a) as c_a:
        create_resp = await c_a.post(
            "/api/v1/coworkers",
            json={
                "name": "secret-helper",
                "folder": _folder("ten-a"),
                "agent_backend": "claude",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        list_a = await c_a.get("/api/v1/coworkers")
        assert list_a.status_code == 200
        names_a = {c["name"] for c in list_a.json()["items"]}
        assert "secret-helper" in names_a

    async with _client(app_b) as c_b:
        list_b = await c_b.get("/api/v1/coworkers")
        assert list_b.status_code == 200
        names_b = {c["name"] for c in list_b.json()["items"]}
        assert "secret-helper" not in names_b
