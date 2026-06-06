"""Integration tests for ``/api/v1/platform/credentials`` (credential pool §5).

Pins the platform pool surface:

- ``credential.pool.manage`` gating: a tenant ``owner`` is denied 403;
  only ``platform_admin`` reaches the routes.
- INV-VAULT-3 carries over: the response never returns the secret, the
  BYTEA column holds ciphertext, and the same vault decrypts it.
- DELETE returns 404 when no pool key exists for the provider.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.credential_vault import (
    CredentialVault,
    set_credential_vault,
)
from rolemesh.auth.encryption import derive_fernet_key
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import _get_admin_pool, create_tenant, create_user
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


@pytest.fixture
def vault() -> CredentialVault:
    v = CredentialVault(derive_fernet_key("test-vault-key"))
    set_credential_vault(v)
    yield v
    set_credential_vault(None)


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


async def _make_user(role: str, slug: str = "plat") -> AuthenticatedUser:
    t = await create_tenant(
        name=f"T-{slug}", slug=f"{slug}-{uuid.uuid4().hex[:8]}",
    )
    u = await create_user(
        tenant_id=t.id, name="Op",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role=role, email="x@x.com", name="X",
    )


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------


async def test_owner_is_forbidden_on_all_platform_routes(vault):
    """A tenant owner lacks ``credential.pool.manage`` → 403 everywhere."""
    user = await _make_user("owner")
    async with _client(_build_app(user)) as ac:
        h = {"Authorization": "Bearer x"}
        assert (await ac.get("/api/v1/platform/credentials", headers=h)).status_code == 403
        put = await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": "sk-x"}, headers=h,
        )
        assert put.status_code == 403
        delete = await ac.delete(
            "/api/v1/platform/credentials/anthropic", headers=h,
        )
        assert delete.status_code == 403


# ---------------------------------------------------------------------------
# Happy path (platform_admin)
# ---------------------------------------------------------------------------


async def test_platform_admin_put_then_get_metadata_only(vault):
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        h = {"Authorization": "Bearer x"}
        put = await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": "sk-platform-secret"}, headers=h,
        )
        assert put.status_code == 200, put.text
        body = put.json()
        assert body["provider"] == "anthropic"
        assert "created_at" in body and "updated_at" in body
        # No secret on the response surface.
        assert "sk-platform-secret" not in put.text

        listing = await ac.get("/api/v1/platform/credentials", headers=h)
        assert listing.status_code == 200
        rows = listing.json()
        assert len(rows) == 1
        assert rows[0]["provider"] == "anthropic"
        assert "credential_data" not in rows[0]
        assert "api_key" not in rows[0]
        assert "sk-platform-secret" not in listing.text


async def test_platform_put_writes_ciphertext_not_plaintext(vault):
    user = await _make_user("platform_admin")
    sentinel = f"SENTINEL_{uuid.uuid4().hex}"
    async with _client(_build_app(user)) as ac:
        put = await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": sentinel},
            headers={"Authorization": "Bearer x"},
        )
        assert put.status_code == 200, put.text

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT credential_data FROM platform_provider_credentials "
            "WHERE provider = 'anthropic'",
        )
    assert row is not None
    blob = bytes(row["credential_data"])
    assert sentinel.encode() not in blob
    assert vault.decrypt_json(blob)["api_key"] == sentinel


async def test_platform_put_overwrites_existing(vault):
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        h = {"Authorization": "Bearer x"}
        first = await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": "first"}, headers=h,
        )
        second = await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": "second"}, headers=h,
        )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["created_at"] == first.json()["created_at"]

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT credential_data FROM platform_provider_credentials "
            "WHERE provider = 'anthropic'",
        )
    assert len(rows) == 1
    assert vault.decrypt_json(bytes(rows[0]["credential_data"]))["api_key"] == "second"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


async def test_platform_delete_succeeds_then_404(vault):
    user = await _make_user("platform_admin")
    async with _client(_build_app(user)) as ac:
        h = {"Authorization": "Bearer x"}
        await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": "sk-x"}, headers=h,
        )
        first = await ac.delete("/api/v1/platform/credentials/anthropic", headers=h)
        assert first.status_code == 204
        second = await ac.delete("/api/v1/platform/credentials/anthropic", headers=h)
        assert second.status_code == 404
        assert second.json()["code"] == "NOT_FOUND"


async def test_platform_credentials_not_visible_to_tenant_credentials_list(vault):
    """A platform pool key does not leak into the tenant credentials list.

    The two surfaces are separate tables; a tenant GET must show only
    its own rows (none here) even after the platform configures a key.
    """
    admin = await _make_user("platform_admin", "admin")
    tenant = await _make_user("owner", "tenant")
    async with _client(_build_app(admin)) as ac:
        await ac.put(
            "/api/v1/platform/credentials/anthropic",
            json={"api_key": "sk-platform"},
            headers={"Authorization": "Bearer x"},
        )
    async with _client(_build_app(tenant)) as ac:
        listing = await ac.get(
            "/api/v1/credentials",
            headers={"Authorization": "Bearer x"},
        )
    assert listing.status_code == 200
    assert listing.json() == []
