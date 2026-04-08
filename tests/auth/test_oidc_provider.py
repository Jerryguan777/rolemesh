"""Tests for OIDCAuthProvider — pure unit tests with mocked HTTPX and DB.

No testcontainer dependency. All DB calls and HTTPX calls are mocked.
"""

from __future__ import annotations

import base64
import importlib
import json
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# ---------------------------------------------------------------------------
# RSA key + JWT factory fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def public_jwk(rsa_key):
    pub = rsa_key.public_key().public_numbers()

    def _b64(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    return {
        "kty": "RSA",
        "kid": "test-key-1",
        "use": "sig",
        "alg": "RS256",
        "n": _b64(pub.n),
        "e": _b64(pub.e),
    }


@pytest.fixture
def make_token(rsa_key):
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def _make(claims: dict) -> str:
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "test-key-1"})

    return _make


# ---------------------------------------------------------------------------
# Mocked httpx — JWKS / discovery
# ---------------------------------------------------------------------------


class _MockResp:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200
        self.text = json.dumps(data)

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class _MockClient:
    def __init__(self, routes: dict[str, dict]) -> None:
        self._routes = routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url: str, **_kwargs):
        if url not in self._routes:
            raise RuntimeError(f"unmocked GET {url}")
        return _MockResp(self._routes[url])


@pytest.fixture
def patch_jwks(public_jwk):
    routes = {
        "https://test.example.com/.well-known/openid-configuration": {
            "issuer": "https://test.example.com/",
            "authorization_endpoint": "https://test.example.com/authorize",
            "token_endpoint": "https://test.example.com/token",
            "jwks_uri": "https://test.example.com/.well-known/jwks.json",
        },
        "https://test.example.com/.well-known/jwks.json": {"keys": [public_jwk]},
    }

    def _factory(*_args, **_kwargs):
        return _MockClient(routes)

    with patch("rolemesh.auth.oidc_provider.httpx.AsyncClient", _factory):
        yield


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv(
        "OIDC_DISCOVERY_URL", "https://test.example.com/.well-known/openid-configuration"
    )
    monkeypatch.setenv("OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("OIDC_AUDIENCE", "test-client")
    import webui.config

    importlib.reload(webui.config)
    yield


# ---------------------------------------------------------------------------
# Mocked DB layer
# ---------------------------------------------------------------------------


class _FakeTenant:
    def __init__(self, id: str, name: str = "Test", slug: str | None = None) -> None:
        self.id = id
        self.name = name
        self.slug = slug


class _FakeUser:
    def __init__(
        self,
        id: str,
        tenant_id: str,
        name: str,
        email: str | None = None,
        role: str = "member",
        external_sub: str | None = None,
    ) -> None:
        self.id = id
        self.tenant_id = tenant_id
        self.name = name
        self.email = email
        self.role = role
        self.external_sub = external_sub


@pytest.fixture
def mock_db():
    """Patch all rolemesh.db.pg functions used by OIDCAuthProvider."""
    state: dict[str, object] = {
        "users_by_sub": {},  # external_sub -> _FakeUser
        "tenants_by_slug": {"default": _FakeTenant("default-tenant-id", slug="default")},
        "tenant_map": {},  # (provider, external_id) -> local_id
        "next_user_id": [100],
        "next_tenant_id": [200],
    }

    async def get_tenant_by_slug(slug: str):
        return state["tenants_by_slug"].get(slug)

    async def create_tenant(name: str, slug: str | None = None, **_kwargs):
        tid = f"tenant-{state['next_tenant_id'][0]}"
        state["next_tenant_id"][0] += 1
        t = _FakeTenant(tid, name=name, slug=slug)
        if slug:
            state["tenants_by_slug"][slug] = t
        return t

    async def get_local_tenant_id(provider: str, external_id: str):
        return state["tenant_map"].get((provider, external_id))

    async def create_external_tenant_mapping(provider: str, external_id: str, local_id: str):
        state["tenant_map"][(provider, external_id)] = local_id

    async def get_user_by_external_sub(external_sub: str):
        return state["users_by_sub"].get(external_sub)

    async def create_user_with_external_sub(
        tenant_id: str, name: str, email, role: str, external_sub: str
    ):
        uid = f"user-{state['next_user_id'][0]}"
        state["next_user_id"][0] += 1
        u = _FakeUser(uid, tenant_id, name, email, role, external_sub)
        state["users_by_sub"][external_sub] = u
        return u

    async def update_user(user_id: str, **fields):
        for u in state["users_by_sub"].values():
            if u.id == user_id:
                for k, v in fields.items():
                    if v is not None:
                        setattr(u, k, v)
                return u
        return None

    async def get_user(user_id: str):
        for u in state["users_by_sub"].values():
            if u.id == user_id:
                return u
        return None

    with (
        patch("rolemesh.db.pg.get_tenant_by_slug", AsyncMock(side_effect=get_tenant_by_slug)),
        patch("rolemesh.db.pg.create_tenant", AsyncMock(side_effect=create_tenant)),
        patch("rolemesh.db.pg.get_local_tenant_id", AsyncMock(side_effect=get_local_tenant_id)),
        patch(
            "rolemesh.db.pg.create_external_tenant_mapping",
            AsyncMock(side_effect=create_external_tenant_mapping),
        ),
        patch(
            "rolemesh.db.pg.get_user_by_external_sub",
            AsyncMock(side_effect=get_user_by_external_sub),
        ),
        patch(
            "rolemesh.db.pg.create_user_with_external_sub",
            AsyncMock(side_effect=create_user_with_external_sub),
        ),
        patch("rolemesh.db.pg.update_user", AsyncMock(side_effect=update_user)),
        patch("rolemesh.db.pg.get_user", AsyncMock(side_effect=get_user)),
    ):
        yield state


# ---------------------------------------------------------------------------
# DefaultOIDCAdapter — pure logic
# ---------------------------------------------------------------------------


def test_default_adapter_direct_role_claim(monkeypatch):
    monkeypatch.setenv("OIDC_CLAIM_ROLE", "role")
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter

    adapter = DefaultOIDCAdapter.from_env()
    assert adapter.map_role({"role": "owner"}) == "owner"
    assert adapter.map_role({"role": "admin"}) == "admin"
    assert adapter.map_role({"role": "member"}) == "member"
    assert adapter.map_role({"role": "junk"}) == "member"
    assert adapter.map_role({}) == "member"


def test_default_adapter_scope_mapping(monkeypatch):
    monkeypatch.setenv(
        "OIDC_SCOPE_ROLE_MAP", '{"admin":"owner","write":"admin","read":"member"}'
    )
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter

    adapter = DefaultOIDCAdapter.from_env()
    assert adapter.map_role({"scope": "read write admin"}) == "owner"
    assert adapter.map_role({"scope": "read write"}) == "admin"
    assert adapter.map_role({"scope": "read"}) == "member"
    assert adapter.map_role({"scope": "unknown"}) == "member"


def test_default_adapter_role_claim_priority_over_scope(monkeypatch):
    monkeypatch.setenv("OIDC_CLAIM_ROLE", "role")
    monkeypatch.setenv("OIDC_SCOPE_ROLE_MAP", '{"read":"member"}')
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter

    adapter = DefaultOIDCAdapter.from_env()
    assert adapter.map_role({"role": "owner", "scope": "read"}) == "owner"


def test_default_adapter_tenant_claim(monkeypatch):
    monkeypatch.setenv("OIDC_CLAIM_TENANT_ID", "tid")
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter

    adapter = DefaultOIDCAdapter.from_env()
    assert adapter.map_tenant_id({"tid": "tenant-42"}) == "tenant-42"
    assert adapter.map_tenant_id({}) == ""


def test_default_adapter_no_tenant_claim_configured(monkeypatch):
    monkeypatch.delenv("OIDC_CLAIM_TENANT_ID", raising=False)
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter

    adapter = DefaultOIDCAdapter.from_env()
    assert adapter.map_tenant_id({"tid": "tenant-42"}) == ""


# ---------------------------------------------------------------------------
# JWKSManager — discovery + key fetching (httpx mocked)
# ---------------------------------------------------------------------------


async def test_jwks_manager_discovery_and_key_fetch(patch_jwks, oidc_env):
    from rolemesh.auth.oidc_provider import JWKSManager

    mgr = JWKSManager("https://test.example.com/.well-known/openid-configuration")
    disc = await mgr.discovery()
    assert disc.issuer == "https://test.example.com/"
    assert disc.token_endpoint == "https://test.example.com/token"
    key = await mgr.get_signing_key("test-key-1")
    assert key is not None


async def test_jwks_manager_unknown_kid_raises(patch_jwks, oidc_env):
    from rolemesh.auth.oidc_provider import JWKSManager

    mgr = JWKSManager("https://test.example.com/.well-known/openid-configuration")
    with pytest.raises(jwt.InvalidTokenError):
        await mgr.get_signing_key("nonexistent-kid")


# ---------------------------------------------------------------------------
# OIDCAuthProvider — full token validation + JIT provisioning (mocked DB)
# ---------------------------------------------------------------------------


async def test_oidc_authenticate_creates_user_in_default_tenant(
    patch_jwks, oidc_env, mock_db, make_token
):
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    token = make_token(
        {
            "sub": "user-12345",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Alice",
            "email": "alice@example.com",
        }
    )
    user = await provider.authenticate(token)
    assert user is not None
    assert user.name == "Alice"
    assert user.email == "alice@example.com"
    assert user.role == "member"
    assert user.tenant_id == "default-tenant-id"
    # Re-auth finds existing
    user2 = await provider.authenticate(token)
    assert user2 is not None and user2.user_id == user.user_id


async def test_oidc_authenticate_jit_provisions_tenant(
    patch_jwks, oidc_env, mock_db, make_token, monkeypatch
):
    monkeypatch.setenv("OIDC_CLAIM_TENANT_ID", "tid")
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    token = make_token(
        {
            "sub": "user-tenant-test",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "tid": "external-tenant-77",
            "name": "Bob",
        }
    )
    user = await provider.authenticate(token)
    assert user is not None
    # New tenant created and mapped
    assert user.tenant_id != "default-tenant-id"
    assert mock_db["tenant_map"][("oidc", "external-tenant-77")] == user.tenant_id


async def test_oidc_authenticate_rejects_wrong_audience(
    patch_jwks, oidc_env, mock_db, make_token
):
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    token = make_token(
        {
            "sub": "user-bad-aud",
            "iss": "https://test.example.com/",
            "aud": "different-client",
            "exp": 9999999999,
            "iat": 1000000000,
        }
    )
    assert await provider.authenticate(token) is None


async def test_oidc_authenticate_rejects_wrong_issuer(
    patch_jwks, oidc_env, mock_db, make_token
):
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    token = make_token(
        {
            "sub": "user-bad-iss",
            "iss": "https://attacker.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
        }
    )
    assert await provider.authenticate(token) is None


async def test_oidc_authenticate_rejects_expired_token(
    patch_jwks, oidc_env, mock_db, make_token
):
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    token = make_token(
        {
            "sub": "user-expired",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 100,
            "iat": 50,
        }
    )
    assert await provider.authenticate(token) is None


async def test_oidc_authenticate_rejects_garbage_token(patch_jwks, oidc_env, mock_db):
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    assert await provider.authenticate("not-a-jwt") is None
    assert await provider.authenticate("") is None


async def test_oidc_authenticate_updates_existing_user_role(
    patch_jwks, oidc_env, mock_db, make_token, monkeypatch
):
    monkeypatch.setenv("OIDC_CLAIM_ROLE", "role")
    from rolemesh.auth.oidc_provider import DefaultOIDCAdapter, OIDCAuthProvider

    provider = OIDCAuthProvider(discovery_url="https://test.example.com/.well-known/openid-configuration", client_id="test-client", audience="test-client", adapter=DefaultOIDCAdapter.from_env())
    # First login as member
    token_v1 = make_token(
        {
            "sub": "user-promote",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Carol",
            "role": "member",
        }
    )
    user1 = await provider.authenticate(token_v1)
    assert user1 is not None and user1.role == "member"

    # Second login as admin → role updated
    token_v2 = make_token(
        {
            "sub": "user-promote",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Carol",
            "role": "admin",
        }
    )
    user2 = await provider.authenticate(token_v2)
    assert user2 is not None
    assert user2.user_id == user1.user_id  # same user
    assert user2.role == "admin"  # updated
