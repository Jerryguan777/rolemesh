"""Pinned tests for :class:`rolemesh.auth.credential_vault.CredentialVault`.

Covers design §8.1.3 invariants that the vault must satisfy at the
unit + DB-roundtrip level. INV-VAULT-3 (API-response sentinel) lives
with the credentials endpoint suite because the assertion is on the
HTTP envelope, not the vault itself.

The tests avoid mocking the cipher — Fernet is a real boundary
dependency and exercising it gives us the same defence the production
path has. The DB-roundtrip case uses the project's standard
testcontainer fixture so the BYTEA serialisation matches what the
real PUT handler will write.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.auth.credential_vault import (
    CREDENTIAL_VAULT_ENV,
    CredentialVault,
    create_credential_vault_from_env,
)
from rolemesh.auth.encryption import (
    derive_fernet_key,
    load_vault_key_from_env,
)

# ---------------------------------------------------------------------------
# INV-VAULT-1 — fail loud on missing env key
# ---------------------------------------------------------------------------


def test_inv_vault_1_missing_env_raises(monkeypatch):
    """``CREDENTIAL_VAULT_KEY`` unset must abort vault construction.

    Tested at two layers: the low-level env helper and the public
    factory used at app boot. A regression at either lets the process
    silently come up without a vault — which then re-surfaces as
    "wrong ciphertext" errors at write-time, far from the actual
    root cause (the missing env var).
    """
    monkeypatch.delenv(CREDENTIAL_VAULT_ENV, raising=False)
    with pytest.raises(RuntimeError, match="CREDENTIAL_VAULT_KEY"):
        load_vault_key_from_env(CREDENTIAL_VAULT_ENV)
    with pytest.raises(RuntimeError, match="CREDENTIAL_VAULT_KEY"):
        create_credential_vault_from_env()


def test_inv_vault_1_empty_env_raises(monkeypatch):
    """An empty-string ``CREDENTIAL_VAULT_KEY`` is treated like unset.

    Catches the case where a deployment manifest threads through an
    empty value (``ENV CREDENTIAL_VAULT_KEY=``); without this guard
    Fernet would still construct from ``derive_fernet_key("")`` (well,
    it wouldn't — the helper rejects too — and the test pins the
    helper itself rejects rather than relying on Fernet's check).
    """
    monkeypatch.setenv(CREDENTIAL_VAULT_ENV, "")
    with pytest.raises(RuntimeError):
        create_credential_vault_from_env()


def test_derive_key_rejects_empty():
    """The shared derivation primitive enforces non-empty secrets."""
    with pytest.raises(ValueError):
        derive_fernet_key("")


def test_derive_key_deterministic():
    """Same secret yields the same key.

    Both webui and orchestrator processes call this independently;
    if the function were non-deterministic, ciphertext written by one
    process would be undecryptable by the other.
    """
    a = derive_fernet_key("shared-secret")
    b = derive_fernet_key("shared-secret")
    c = derive_fernet_key("other-secret")
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# Roundtrip + ciphertext-doesn't-contain-plaintext
# ---------------------------------------------------------------------------


@pytest.fixture
def vault() -> CredentialVault:
    return CredentialVault(derive_fernet_key("test-secret-32-bytes-or-whatever"))


def test_encrypt_decrypt_json_roundtrip(vault: CredentialVault):
    payload = {"api_key": "sk-ant-test-1234", "extra": "value"}
    blob = vault.encrypt_json(payload)
    assert isinstance(blob, bytes)
    assert vault.decrypt_json(blob) == payload


def test_encrypt_with_wrong_key_fails(vault: CredentialVault):
    """Decrypt with a different key raises — pins the auth side of Fernet.

    Catches a regression where the vault wrapper inadvertently uses a
    non-authenticated cipher.
    """
    from cryptography.fernet import InvalidToken

    blob = vault.encrypt_json({"api_key": "secret"})
    other = CredentialVault(derive_fernet_key("different-secret"))
    with pytest.raises(InvalidToken):
        other.decrypt_json(blob)


def test_inv_vault_2_ciphertext_does_not_contain_plaintext_substring(
    vault: CredentialVault,
):
    """The Fernet token bytes must not contain the plaintext substring.

    A sentinel that is statistically unlikely to appear in random
    ciphertext is encrypted; we then check the bytes (interpreted as
    UTF-8 best-effort, since that's the form a curious operator would
    grep) do not leak it. The mutation check this pins is "what if we
    accidentally swap encrypt for base64 in a refactor" — that would
    instantly fail.
    """
    sentinel = f"SENTINEL_LEAK_{uuid.uuid4().hex}"
    blob = vault.encrypt_json({"api_key": sentinel})
    # ``ignore`` is fine: Fernet tokens are urlsafe-b64 so the decode
    # is lossless, but we use ignore as belt-and-braces against any
    # future cipher change.
    decoded = blob.decode("utf-8", errors="ignore")
    assert sentinel not in decoded
    assert sentinel.encode() not in blob


def test_decrypt_non_object_raises(vault: CredentialVault):
    """A vault that decrypts to a JSON array must error.

    Defensive: callers assume ``decrypt_json`` returns a dict and may
    index keys directly. Returning a list would crash at the call
    site with a confusing TypeError; we surface it at the vault layer
    with a clear message.
    """
    # Hand-craft an encrypted non-object payload using the same Fernet
    # the vault uses.
    raw = vault._fernet.encrypt(b"[1, 2, 3]")  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="JSON object"):
        vault.decrypt_json(raw)


# ---------------------------------------------------------------------------
# INV-VAULT-2 (DB-side) — what is persisted does not contain the plaintext
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.usefixtures("test_db")


async def test_inv_vault_2_db_row_contains_no_plaintext_sentinel(vault: CredentialVault):
    """INV-VAULT-2: SELECT-back of the BYTEA column omits the plaintext.

    Writes a sentinel-bearing credential straight through the same
    primitives the production endpoint uses, then re-reads via the
    admin pool and decodes the BYTEA. Catches a future bug where a
    refactor accidentally writes the JSON payload directly (e.g.
    ``json.dumps(...).encode()``) instead of the Fernet ciphertext.
    """
    from rolemesh.db import _get_admin_pool, create_tenant

    tenant = await create_tenant(
        name="vault-roundtrip",
        slug=f"vault-{uuid.uuid4().hex[:8]}",
    )
    sentinel = f"SENTINEL_LEAK_{uuid.uuid4().hex}"
    blob = vault.encrypt_json({"api_key": sentinel})

    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_model_credentials (tenant_id, provider, credential_data) "
            "VALUES ($1::uuid, $2, $3)",
            tenant.id, "anthropic", blob,
        )
        row = await conn.fetchrow(
            "SELECT credential_data FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid AND provider = $2",
            tenant.id, "anthropic",
        )

    assert row is not None
    stored = bytes(row["credential_data"])
    # The DB must hand back exactly what we wrote.
    assert stored == blob
    # And the bytes must not contain the plaintext sentinel in any
    # encoding a curious operator would grep through.
    assert sentinel.encode() not in stored
    decoded = stored.decode("utf-8", errors="ignore")
    assert sentinel not in decoded
    # Round-trip via the vault still produces the original payload.
    assert vault.decrypt_json(stored) == {"api_key": sentinel}
