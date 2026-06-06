"""Tests for :mod:`rolemesh.egress.credentials` ``CredentialResolver``.

Exercised against a real Postgres (testcontainer via the ``test_db``
fixture) and a real :class:`CredentialVault` — no mocks of internal
modules. The only ``mock`` surface is ``wraps=`` spying on
``vault.decrypt_json`` to count calls without replacing the real
Fernet path.

Each test names the mutation it pins (see module-level
docstring of egress/credentials.py for the contract).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from rolemesh.auth.credential_vault import CredentialVault
from rolemesh.db import _get_admin_pool, create_tenant
from rolemesh.egress.credentials import (
    CredentialResolver,
    MissingCredentialError,
)

pytestmark = [pytest.mark.usefixtures("test_db"), pytest.mark.asyncio]


@pytest.fixture
def vault() -> CredentialVault:
    """A fresh vault per test — key randomness rules out cross-test bleed."""
    return CredentialVault(Fernet.generate_key())


async def _new_tenant(slug_hint: str = "cred-res") -> str:
    t = await create_tenant(
        name=f"T-{slug_hint}",
        slug=f"{slug_hint}-{uuid.uuid4().hex[:8]}",
    )
    return t.id


async def _write_cred(
    tenant_id: str, provider: str, blob: bytes
) -> None:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_model_credentials "
            "(tenant_id, provider, credential_data) "
            "VALUES ($1::uuid, $2, $3)",
            tenant_id, provider, blob,
        )


async def _write_pool_row(
    tenant_id: str, provider: str, dormant_blob: bytes | None = None
) -> None:
    """Insert a tenant row in ``pool`` mode (optionally with a dormant key)."""
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tenant_model_credentials "
            "(tenant_id, provider, credential_mode, credential_data) "
            "VALUES ($1::uuid, $2, 'pool', $3)",
            tenant_id, provider, dormant_blob,
        )


async def _write_platform_cred(provider: str, blob: bytes) -> None:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO platform_provider_credentials "
            "(provider, credential_data) VALUES ($1, $2)",
            provider, blob,
        )


# ---------------------------------------------------------------------------
# Test 1 — happy path: decrypt round-trip
# ---------------------------------------------------------------------------


async def test_resolve_returns_decrypted_credential(vault: CredentialVault):
    """Pin: resolver actually Fernet-decrypts the DB ciphertext.

    Mutation: if ``resolve`` returns ``blob`` (bytes) instead of the
    decrypted dict, the dict-style indexing in the assertion fails.
    """
    tenant_id = await _new_tenant()
    payload = {"api_key": "sk-test-decrypt-roundtrip", "extras": {"x": 1}}
    await _write_cred(tenant_id, "anthropic", vault.encrypt_json(payload))

    resolver = CredentialResolver(vault)

    result = await resolver.resolve(tenant_id, "anthropic")

    assert result == payload
    assert result["api_key"] == "sk-test-decrypt-roundtrip"
    assert result["extras"] == {"x": 1}


# ---------------------------------------------------------------------------
# Test 2 — DB miss raises MissingCredentialError
# ---------------------------------------------------------------------------


async def test_resolve_missing_raises_missing_credential(
    vault: CredentialVault,
):
    """Pin: absent row raises ``MissingCredentialError``, not silent None.

    Mutation: returning ``None`` on miss (silent fallback path —
    the very bug this whole chore exists to fix) fails ``pytest.raises``.
    """
    tenant_id = await _new_tenant()
    # No row written.

    resolver = CredentialResolver(vault)

    with pytest.raises(MissingCredentialError) as exc_info:
        await resolver.resolve(tenant_id, "anthropic")

    assert exc_info.value.tenant_id == tenant_id
    assert exc_info.value.provider == "anthropic"


# ---------------------------------------------------------------------------
# Test 3 — cache hit skips vault decrypt
# ---------------------------------------------------------------------------


async def test_resolve_cache_hit_skips_vault_decrypt(
    vault: CredentialVault,
):
    """Pin: second ``resolve`` for the same (tenant, provider) reuses cache.

    Spies on ``vault.decrypt_json`` with ``wraps=`` so the real Fernet
    path still runs but call count is observable.

    Mutation: removing the cache lookup branch makes ``decrypt_json``
    fire twice; assertion ``== 1`` fails.
    """
    tenant_id = await _new_tenant()
    await _write_cred(
        tenant_id, "anthropic", vault.encrypt_json({"api_key": "k"}),
    )

    resolver = CredentialResolver(vault)

    with patch.object(
        vault, "decrypt_json", wraps=vault.decrypt_json
    ) as spy:
        await resolver.resolve(tenant_id, "anthropic")
        await resolver.resolve(tenant_id, "anthropic")

    assert spy.call_count == 1


# ---------------------------------------------------------------------------
# Test 4 — TTL expiry forces re-decrypt
# ---------------------------------------------------------------------------


async def test_resolve_cache_expires_after_ttl(vault: CredentialVault):
    """Pin: ``ttl_seconds=0`` invalidates the cache on every call.

    Mutation: dropping the ``cached[1] > now`` check (always-cache
    bug) keeps ``decrypt_json`` at 1 call instead of 2.
    """
    tenant_id = await _new_tenant()
    await _write_cred(
        tenant_id, "anthropic", vault.encrypt_json({"api_key": "k"}),
    )

    resolver = CredentialResolver(vault, ttl_seconds=0)

    with patch.object(
        vault, "decrypt_json", wraps=vault.decrypt_json
    ) as spy:
        await resolver.resolve(tenant_id, "anthropic")
        await resolver.resolve(tenant_id, "anthropic")

    assert spy.call_count == 2


# ---------------------------------------------------------------------------
# Test 5 — tenant isolation
# ---------------------------------------------------------------------------


async def test_resolve_isolates_tenants(vault: CredentialVault):
    """Pin: ``(tenant_id, provider)`` is the cache key — not provider alone.

    Mutation: caching by ``provider`` only would let tenant B's resolve
    return tenant A's cached dict; the per-tenant assertions catch it
    on both directions (A→B and B→A interleaving covered by reversal).
    """
    tenant_a = await _new_tenant("alpha")
    tenant_b = await _new_tenant("beta")

    await _write_cred(
        tenant_a, "anthropic", vault.encrypt_json({"api_key": "K_A"}),
    )
    await _write_cred(
        tenant_b, "anthropic", vault.encrypt_json({"api_key": "K_B"}),
    )

    resolver = CredentialResolver(vault)

    # Order matters for catching the mutation: A first warms the
    # cache; B's resolve must NOT see A's value.
    a_first = await resolver.resolve(tenant_a, "anthropic")
    b_first = await resolver.resolve(tenant_b, "anthropic")
    a_again = await resolver.resolve(tenant_a, "anthropic")
    b_again = await resolver.resolve(tenant_b, "anthropic")

    assert a_first["api_key"] == "K_A"
    assert b_first["api_key"] == "K_B"
    assert a_again["api_key"] == "K_A"
    assert b_again["api_key"] == "K_B"


# ---------------------------------------------------------------------------
# Test 6 — exception message carries only identifiers
# ---------------------------------------------------------------------------


async def test_missing_credential_exception_carries_only_identifiers(
    vault: CredentialVault,
):
    """Pin: ``MissingCredentialError`` repr never contains secret-shaped data.

    The constructor takes only ``tenant_id`` and ``provider`` by
    design. This test locks in the contract: a future maintainer who
    extends the exception with a ``api_key`` arg "for debugging"
    breaks this assertion before they ship.

    Mutation: passing extra plaintext-shaped args into the exception
    (e.g. attaching the API key) trips the substring assertions.
    """
    tenant_id = await _new_tenant()
    resolver = CredentialResolver(vault)

    with pytest.raises(MissingCredentialError) as exc_info:
        await resolver.resolve(tenant_id, "anthropic")

    rendered = f"{exc_info.value!s} {exc_info.value!r}"

    # Identifiers visible — present-and-correct mutations (e.g.
    # silently dropping tenant_id from the message) also fail.
    assert tenant_id in rendered
    assert "anthropic" in rendered

    # Secret-shaped substrings must never appear. These literals
    # come from the public Anthropic / generic OAuth conventions —
    # they are the things a maintainer would copy/paste in.
    for forbidden in (
        "sk-",
        "Bearer ",
        "api_key=",
        "credential_data",
        "ciphertext",
    ):
        assert forbidden not in rendered, (
            f"MissingCredentialError leaked '{forbidden}'-shaped substring"
        )


# ---------------------------------------------------------------------------
# Test 7 — pool mode resolves the platform key
# ---------------------------------------------------------------------------


async def test_resolve_pool_mode_uses_platform_key(vault: CredentialVault):
    """Pin: a ``pool`` row decrypts the platform pool key, not a tenant key.

    Mutation: routing every row through the tenant ciphertext (ignoring
    ``credential_mode``) returns the wrong payload (or raises, since the
    pool row has no key) and fails the equality assertion.
    """
    tenant_id = await _new_tenant("pool")
    platform_payload = {"api_key": "sk-platform-pool-key"}
    await _write_platform_cred("anthropic", vault.encrypt_json(platform_payload))
    await _write_pool_row(tenant_id, "anthropic")  # no dormant key

    resolver = CredentialResolver(vault)

    result = await resolver.resolve(tenant_id, "anthropic")

    assert result == platform_payload


# ---------------------------------------------------------------------------
# Test 8 — pool mode without a platform key fails closed
# ---------------------------------------------------------------------------


async def test_resolve_pool_mode_without_platform_key_raises(
    vault: CredentialVault,
):
    """Pin: electing pool when the platform has no key is a hard miss.

    Mutation: treating a missing pool key as "fall back to anything"
    (or returning None) fails ``pytest.raises``.
    """
    tenant_id = await _new_tenant("pool-empty")
    await _write_pool_row(tenant_id, "anthropic")  # no platform key configured

    resolver = CredentialResolver(vault)

    with pytest.raises(MissingCredentialError):
        await resolver.resolve(tenant_id, "anthropic")


# ---------------------------------------------------------------------------
# Test 9 — dormant BYOK key is ignored while mode is pool
# ---------------------------------------------------------------------------


async def test_resolve_pool_mode_ignores_dormant_byok_key(
    vault: CredentialVault,
):
    """Pin: a pool row's retained BYOK ciphertext never wins over the pool.

    A byok→pool switch keeps the old key dormant so the tenant can flip
    back. The resolver must still serve the *platform* key while mode is
    pool.

    Mutation: reading ``credential_data`` before checking the platform
    pool returns the dormant tenant key and fails the assertion.
    """
    tenant_id = await _new_tenant("dormant")
    dormant = vault.encrypt_json({"api_key": "sk-OLD-tenant-key"})
    platform_payload = {"api_key": "sk-platform-pool-key"}
    await _write_platform_cred("anthropic", vault.encrypt_json(platform_payload))
    await _write_pool_row(tenant_id, "anthropic", dormant_blob=dormant)

    resolver = CredentialResolver(vault)

    result = await resolver.resolve(tenant_id, "anthropic")

    assert result == platform_payload
    assert result["api_key"] != "sk-OLD-tenant-key"
