"""Tests for TokenVault — encrypted per-user OIDC token storage with auto refresh.

Uses mocked httpx (IdP token endpoint) and mocked DB. No testcontainer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from rolemesh.auth.token_vault import TokenVault

# ---------------------------------------------------------------------------
# Mocked httpx
# ---------------------------------------------------------------------------


class _MockResp:
    def __init__(self, data: dict, status: int = 200) -> None:
        self._data = data
        self.status_code = status
        self.text = json.dumps(data)

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class _MockClient:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url: str, **_kwargs):
        if not self._responses:
            raise RuntimeError(f"unmocked POST {url}")
        status, data = self._responses.pop(0)
        return _MockResp(data, status)


@pytest.fixture
def httpx_state():
    state = {"responses": []}

    def _factory(*_args, **_kwargs):
        return _MockClient(state["responses"])

    with patch("rolemesh.auth.token_vault.httpx.AsyncClient", _factory):
        yield state


# ---------------------------------------------------------------------------
# Mocked DB
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db():
    state: dict[str, tuple] = {}

    async def upsert(user_id, refresh_enc, access_enc, expires_at):
        state[user_id] = (refresh_enc, access_enc, expires_at)

    async def get_tokens(user_id):
        return state.get(user_id)

    async def update_access(user_id, access_enc, expires_at):
        if user_id in state:
            old_refresh, _, _ = state[user_id]
            state[user_id] = (old_refresh, access_enc, expires_at)

    async def update_refresh(user_id, refresh_enc):
        if user_id in state:
            _, old_access, old_exp = state[user_id]
            state[user_id] = (refresh_enc, old_access, old_exp)

    async def delete(user_id):
        state.pop(user_id, None)

    with (
        patch("rolemesh.db.upsert_user_oidc_tokens", AsyncMock(side_effect=upsert)),
        patch("rolemesh.db.get_user_oidc_tokens", AsyncMock(side_effect=get_tokens)),
        patch("rolemesh.db.update_user_access_token", AsyncMock(side_effect=update_access)),
        patch("rolemesh.db.update_user_refresh_token", AsyncMock(side_effect=update_refresh)),
        patch("rolemesh.db.delete_user_oidc_tokens", AsyncMock(side_effect=delete)),
    ):
        yield state


# ---------------------------------------------------------------------------
# Vault fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def vault():
    return TokenVault(
        encryption_key=TokenVault.derive_key("test-secret-key-32-chars-long-enough!"),
        idp_token_endpoint="https://idp.test/token",
        client_id="test-client",
        client_secret="",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_derive_key_deterministic():
    k1 = TokenVault.derive_key("same-secret")
    k2 = TokenVault.derive_key("same-secret")
    k3 = TokenVault.derive_key("different")
    assert k1 == k2
    assert k1 != k3


def test_derive_key_empty_raises():
    with pytest.raises(ValueError):
        TokenVault.derive_key("")


def test_encrypt_decrypt_roundtrip(vault):
    cipher = vault._encrypt("hello-world")
    plain = vault._decrypt(cipher)
    assert plain == "hello-world"


async def test_store_initial_persists_encrypted(vault, mock_db):
    await vault.store_initial("user-1", "rt-plain", "at-plain", expires_in=3600)
    assert "user-1" in mock_db
    refresh_enc, access_enc, expires_at = mock_db["user-1"]
    # Encrypted blobs are NOT plaintext
    assert refresh_enc != b"rt-plain"
    assert access_enc != b"at-plain"
    # Decrypts back correctly
    assert vault._decrypt(refresh_enc) == "rt-plain"
    assert vault._decrypt(access_enc) == "at-plain"
    # Expires roughly 1 hour from now
    delta = (expires_at - datetime.now(UTC)).total_seconds()
    assert 3500 < delta < 3700


async def test_get_fresh_returns_cached_when_valid(vault, mock_db, httpx_state):
    await vault.store_initial("user-1", "rt", "at-cached", expires_in=3600)
    token = await vault.get_fresh_access_token("user-1")
    assert token == "at-cached"
    # Should NOT have called IdP
    assert len(httpx_state["responses"]) == 0


async def test_get_fresh_refreshes_when_near_expiry(vault, mock_db, httpx_state):
    # Store with very short TTL → falls under refresh threshold immediately
    await vault.store_initial("user-1", "rt-original", "at-old", expires_in=10)
    httpx_state["responses"].append(
        (200, {"access_token": "at-new", "expires_in": 3600})
    )
    token = await vault.get_fresh_access_token("user-1")
    assert token == "at-new"
    # Cache should be updated
    _, access_enc, _ = mock_db["user-1"]
    assert vault._decrypt(access_enc) == "at-new"


async def test_get_fresh_handles_rotation(vault, mock_db, httpx_state):
    await vault.store_initial("user-1", "rt-old", "at-old", expires_in=10)
    httpx_state["responses"].append(
        (200, {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600})
    )
    await vault.get_fresh_access_token("user-1")
    refresh_enc, _, _ = mock_db["user-1"]
    assert vault._decrypt(refresh_enc) == "rt-new"


async def test_get_fresh_purges_on_refresh_failure(vault, mock_db, httpx_state):
    await vault.store_initial("user-1", "rt-revoked", "at-old", expires_in=10)
    httpx_state["responses"].append((400, {"error": "invalid_grant"}))
    token = await vault.get_fresh_access_token("user-1")
    assert token is None
    # User row should be deleted
    assert "user-1" not in mock_db


async def test_get_fresh_returns_none_for_unknown_user(vault, mock_db, httpx_state):
    token = await vault.get_fresh_access_token("never-seen")
    assert token is None
    assert len(httpx_state["responses"]) == 0


async def test_revoke_deletes_tokens(vault, mock_db):
    await vault.store_initial("user-1", "rt", "at", expires_in=3600)
    assert "user-1" in mock_db
    await vault.revoke("user-1")
    assert "user-1" not in mock_db


async def test_per_user_lock_prevents_concurrent_refresh(vault, mock_db, httpx_state):
    """Two concurrent get_fresh calls for same user → only one IdP call."""
    import asyncio

    await vault.store_initial("user-1", "rt", "at-old", expires_in=10)
    httpx_state["responses"].append(
        (200, {"access_token": "at-new", "expires_in": 3600})
    )
    # Only ONE response queued; if a second call hits IdP, RuntimeError raises
    results = await asyncio.gather(
        vault.get_fresh_access_token("user-1"),
        vault.get_fresh_access_token("user-1"),
    )
    assert results == ["at-new", "at-new"]
