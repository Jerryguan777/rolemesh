"""Integration tests for ``/api/v1/tenant/credentials``.

Pins design §8.1 invariants from the HTTP layer:

- INV-VAULT-3: the response surface never returns plaintext.
- DB write goes through ``CredentialVault.encrypt_json`` — the BYTEA
  column contains ciphertext, not the JSON payload (also covered by
  the dedicated vault test, but re-checked end-to-end here because
  this is where the integration could go sideways).
- DELETE on a referenced credential returns 409 with the offender
  list.

The vault singleton must be installed before each test — the
fixture wires :func:`rolemesh.auth.credential_vault.set_credential_vault`
explicitly because the test app does not run ``webui.main.lifespan``.
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
from rolemesh.db import (
    _get_admin_pool,
    create_tenant,
    create_user,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


@pytest.fixture
def vault() -> CredentialVault:
    """Install a process-wide vault for the duration of one test."""
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


async def _make_user(slug: str = "cred") -> AuthenticatedUser:
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_put_credential_creates_then_get_lists_metadata_only(vault):
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        put = await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "sk-ant-test-1234"},
            headers={"Authorization": "Bearer x"},
        )
        assert put.status_code == 200, put.text
        upserted = put.json()
        assert upserted["provider"] == "anthropic"
        assert "created_at" in upserted and "updated_at" in upserted
        # INV-VAULT-3 — the response payload must not contain the
        # secret in any envelope.
        assert "sk-ant-test-1234" not in put.text

        listing = await ac.get(
            "/api/v1/tenant/credentials",
            headers={"Authorization": "Bearer x"},
        )
        assert listing.status_code == 200
        body = listing.json()
        assert len(body) == 1
        assert body[0]["provider"] == "anthropic"
        # No plaintext / ciphertext / credential_data field on the list.
        assert "credential_data" not in body[0]
        assert "api_key" not in body[0]
        assert "sk-ant-test-1234" not in listing.text


async def test_put_credential_writes_fernet_ciphertext_not_plaintext(vault):
    """Sentinel-grep across the BYTEA column verifies envelope encryption.

    INV-VAULT-2 at the HTTP level: send a sentinel key, then look
    straight at the DB row's ciphertext and confirm it does not
    contain the sentinel. Catches a regression where the handler
    accidentally stores the JSON payload directly (e.g. someone
    rewires the call past the vault).
    """
    user = await _make_user()
    sentinel = f"SENTINEL_LEAK_{uuid.uuid4().hex}"
    async with _client(_build_app(user)) as ac:
        put = await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": sentinel},
            headers={"Authorization": "Bearer x"},
        )
        assert put.status_code == 200, put.text
        assert sentinel not in put.text

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT credential_data FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid AND provider = 'anthropic'",
            user.tenant_id,
        )
    assert row is not None
    blob = bytes(row["credential_data"])
    assert sentinel.encode() not in blob
    decoded = blob.decode("utf-8", errors="ignore")
    assert sentinel not in decoded
    # Decrypts back to the original plaintext.
    assert vault.decrypt_json(blob)["api_key"] == sentinel


async def test_put_credential_overwrites_existing(vault):
    """A second PUT updates the row in place; updated_at advances.

    Catches the "INSERT failed and silently swallowed" regression
    where the ON CONFLICT clause is dropped or rewired.
    """
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        first = await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "first"},
            headers={"Authorization": "Bearer x"},
        )
        second = await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "second"},
            headers={"Authorization": "Bearer x"},
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["created_at"] == first.json()["created_at"]
    assert second.json()["updated_at"] >= first.json()["updated_at"]

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT credential_data FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid AND provider = 'anthropic'",
            user.tenant_id,
        )
    assert len(rows) == 1
    assert vault.decrypt_json(bytes(rows[0]["credential_data"]))["api_key"] == "second"


async def test_put_credential_with_extras_roundtrips(vault):
    """``extras`` survives the vault encryption + storage."""
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.put(
            "/api/v1/tenant/credentials/bedrock",
            json={"api_key": "akia-test", "extras": {"region": "us-east-1"}},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT credential_data FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid AND provider = 'bedrock'",
            user.tenant_id,
        )
    payload = vault.decrypt_json(bytes(row["credential_data"]))
    assert payload == {"api_key": "akia-test", "extras": {"region": "us-east-1"}}


# ---------------------------------------------------------------------------
# DELETE 409
# ---------------------------------------------------------------------------


async def test_delete_credential_returns_409_when_in_use(vault):
    """Reference by a coworker -> 409 RESOURCE_IN_USE with coworker ids."""
    user = await _make_user()
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        # Seed: credential row + model + coworker referencing the same
        # provider.
        model_row = await conn.fetchrow(
            "SELECT id FROM models WHERE provider = 'anthropic' LIMIT 1",
        )
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend, model_id) "
            "VALUES ($1::uuid, $2, $3, $4, $5::uuid) RETURNING id",
            user.tenant_id, "marketing", "marketing", "claude", str(model_row["id"]),
        )

    async with _client(_build_app(user)) as ac:
        await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "sk-test"},
            headers={"Authorization": "Bearer x"},
        )
        resp = await ac.delete(
            "/api/v1/tenant/credentials/anthropic",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "RESOURCE_IN_USE"
    assert str(cw_id) in body["details"]["coworker_ids"]


async def test_delete_credential_succeeds_when_no_references(vault):
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "sk-test"},
            headers={"Authorization": "Bearer x"},
        )
        resp = await ac.delete(
            "/api/v1/tenant/credentials/anthropic",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 204
    # Subsequent GET shows the row is gone.
    async with _client(_build_app(user)) as ac:
        listing = await ac.get(
            "/api/v1/tenant/credentials",
            headers={"Authorization": "Bearer x"},
        )
    assert listing.json() == []


async def test_delete_credential_returns_404_when_unknown(vault):
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.delete(
            "/api/v1/tenant/credentials/anthropic",
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Hot-reload event publishes
# ---------------------------------------------------------------------------


async def test_put_credential_publishes_restart_per_affected_coworker(
    vault, monkeypatch,
):
    """Each coworker using the provider gets one restart event.

    The DB join is the load-bearing piece here; we monkeypatch the
    publisher to capture call args and assert the set matches the
    referenced coworkers. Pins the fan-out: a regression that publishes
    one tenant-level event would still pass the credential write
    happy-path but break orchestrator reload.
    """
    user = await _make_user()
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        anthropic_model = await conn.fetchval(
            "SELECT id FROM models WHERE provider = 'anthropic' LIMIT 1",
        )
        openai_model = await conn.fetchval(
            "SELECT id FROM models WHERE provider = 'openai' LIMIT 1",
        )
        cw_a = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend, model_id) "
            "VALUES ($1::uuid, $2, $3, $4, $5::uuid) RETURNING id",
            user.tenant_id, "anthro-1", "anthro1", "claude", str(anthropic_model),
        )
        cw_b = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend, model_id) "
            "VALUES ($1::uuid, $2, $3, $4, $5::uuid) RETURNING id",
            user.tenant_id, "anthro-2", "anthro2", "claude", str(anthropic_model),
        )
        await conn.execute(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend, model_id) "
            "VALUES ($1::uuid, $2, $3, $4, $5::uuid)",
            user.tenant_id, "gpt-1", "gpt1", "claude", str(openai_model),
        )

    seen: list[dict[str, str]] = []

    async def _capture(*, coworker_id: str, tenant_id: str) -> None:
        seen.append({"coworker_id": coworker_id, "tenant_id": tenant_id})

    monkeypatch.setattr(
        "webui.v1.credentials.coworker_events.publish_coworker_restart",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        resp = await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "sk-test"},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    fired_for = {e["coworker_id"] for e in seen}
    assert fired_for == {str(cw_a), str(cw_b)}
    assert all(e["tenant_id"] == user.tenant_id for e in seen)


async def test_put_credential_does_not_publish_for_unused_provider(
    vault, monkeypatch,
):
    """No coworker references the provider -> no restart events."""
    user = await _make_user()

    seen: list[str] = []

    async def _capture(*, coworker_id: str, tenant_id: str) -> None:
        seen.append(coworker_id)

    monkeypatch.setattr(
        "webui.v1.credentials.coworker_events.publish_coworker_restart",
        _capture,
    )

    async with _client(_build_app(user)) as ac:
        resp = await ac.put(
            "/api/v1/tenant/credentials/openai",
            json={"api_key": "sk-test"},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200
    assert seen == []


# ---------------------------------------------------------------------------
# Log sanitisation — pinned at the helper layer (cheaper than mocking logger)
# ---------------------------------------------------------------------------


def test_log_sanitize_redacts_api_key_nested():
    from webui.v1._log_sanitize import sanitize_for_log

    sanitised = sanitize_for_log(
        {
            "api_key": "sk-leak",
            "extras": {"api_key": "nested-leak", "region": "us-east-1"},
            "list": [{"api_key": "list-leak"}, "scalar"],
        }
    )
    assert sanitised["api_key"] == "<redacted>"
    assert sanitised["extras"]["api_key"] == "<redacted>"
    assert sanitised["extras"]["region"] == "us-east-1"
    assert sanitised["list"][0]["api_key"] == "<redacted>"
    assert sanitised["list"][1] == "scalar"


# ---------------------------------------------------------------------------
# RLS isolation
# ---------------------------------------------------------------------------


async def test_get_credentials_isolated_per_tenant(vault):
    """One tenant cannot see another tenant's rows.

    INV-1 belt-and-braces: the handler uses ``tenant_conn`` and the
    DB query carries the explicit ``WHERE tenant_id``. Both layers
    must agree for the row to surface.
    """
    a = await _make_user("a")
    b = await _make_user("b")
    async with _client(_build_app(a)) as ac:
        await ac.put(
            "/api/v1/tenant/credentials/anthropic",
            json={"api_key": "for-a"},
            headers={"Authorization": "Bearer x"},
        )
    async with _client(_build_app(b)) as ac:
        listing = await ac.get(
            "/api/v1/tenant/credentials",
            headers={"Authorization": "Bearer x"},
        )
    assert listing.status_code == 200
    assert listing.json() == []
