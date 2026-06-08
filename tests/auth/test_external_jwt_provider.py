"""Unit tests for ExternalJwtProvider claim validation.

No DB / network: tokens are signed with a local HS256 secret and the provider
is exercised directly, so these assert the claim-to-identity mapping in
isolation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import jwt

from rolemesh.auth.external_jwt_provider import ExternalJwtProvider

if TYPE_CHECKING:
    import pytest

_SECRET = "unit-test-secret-padded-to-32-bytes-min"


def _provider(monkeypatch: pytest.MonkeyPatch) -> ExternalJwtProvider:
    # The provider snapshots its config in __init__, so set env BEFORE building.
    monkeypatch.setenv("EXTERNAL_JWT_SECRET", _SECRET)
    monkeypatch.setenv("EXTERNAL_JWT_ALGORITHMS", "HS256")
    return ExternalJwtProvider()


def _token(**claims: object) -> str:
    return jwt.encode(claims, _SECRET, algorithm="HS256")


async def test_rejects_token_missing_tenant_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signature-valid token with no tenant claim must not authenticate.

    Regression: it previously yielded ``AuthenticatedUser(tenant_id='')``,
    which a tenant-scoped read could treat as "no tenant scope" and leak
    across tenants. An under-specified token must fail closed at the provider.
    """
    provider = _provider(monkeypatch)
    token = _token(sub="user-1", role="member")  # no tenant ("tid") claim
    assert await provider.authenticate(token) is None


async def test_rejects_token_with_empty_tenant_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    token = _token(sub="user-1", tid="", role="member")
    assert await provider.authenticate(token) is None


async def test_rejects_token_missing_user_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    token = _token(tid="tenant-1", role="member")  # no user ("sub") claim
    assert await provider.authenticate(token) is None


async def test_accepts_token_with_uuid_user_and_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    uid = "11111111-1111-1111-1111-111111111111"
    token = _token(sub=uid, tid="tenant-9", role="owner")
    user = await provider.authenticate(token)
    assert user is not None
    assert user.user_id == uid
    assert user.tenant_id == "tenant-9"
    assert user.role == "owner"


async def test_rejects_token_with_non_uuid_user_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant: AuthenticatedUser.user_id is always a real UUID.

    A non-UUID user-id claim (integer id, ``user_12345``, an email,
    ...) is rejected at the provider boundary rather than carried
    downstream, where it would FK-violate the audit / ``created_by``
    columns as a 500. ``tenant_id`` shape is intentionally not
    validated here.
    """
    provider = _provider(monkeypatch)
    for bad in ("user-1", "12345", "alice@example.com"):
        token = _token(sub=bad, tid="tenant-9", role="member")
        assert await provider.authenticate(token) is None, bad
