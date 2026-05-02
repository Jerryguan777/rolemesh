"""Tests for OIDC PKCE endpoints — exchange, refresh, logout.

Uses FastAPI TestClient with mocked httpx (IdP token endpoint) and
mocked DB. No testcontainer dependency.
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# RSA + JWT factory
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
# Mocked httpx — supports GET (discovery, JWKS) and POST (token endpoint)
# ---------------------------------------------------------------------------


class _MockResp:
    def __init__(self, data: dict, status: int = 200) -> None:
        self._data = data
        self.status_code = status
        self.text = json.dumps(data)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._data


class _MockClient:
    """Mock httpx client. POST routes are stateful — yield from a list."""

    def __init__(
        self,
        get_routes: dict[str, dict],
        post_routes: dict[str, list[tuple[int, dict]]],
    ) -> None:
        self._get_routes = get_routes
        self._post_routes = post_routes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url: str, **_kwargs):
        if url not in self._get_routes:
            raise RuntimeError(f"unmocked GET {url}")
        return _MockResp(self._get_routes[url])

    async def post(self, url: str, **_kwargs):
        queue = self._post_routes.get(url)
        if not queue:
            raise RuntimeError(f"unmocked POST {url}")
        status, data = queue.pop(0)
        return _MockResp(data, status)


@pytest.fixture
def mock_httpx(public_jwk, make_token):
    """Provide a mutable container so each test can stage POST responses."""
    state = {
        "get_routes": {
            "https://test.example.com/.well-known/openid-configuration": {
                "issuer": "https://test.example.com/",
                "authorization_endpoint": "https://test.example.com/authorize",
                "token_endpoint": "https://test.example.com/token",
                "jwks_uri": "https://test.example.com/.well-known/jwks.json",
            },
            "https://test.example.com/.well-known/jwks.json": {"keys": [public_jwk]},
        },
        "post_routes": {
            "https://test.example.com/token": [],  # tests append responses
        },
    }

    def _factory(*_args, **_kwargs):
        return _MockClient(state["get_routes"], state["post_routes"])

    with patch("rolemesh.auth.oidc.jwks.httpx.AsyncClient", _factory), patch(
        "webui.oidc_routes.httpx.AsyncClient", _factory
    ):
        yield state


# ---------------------------------------------------------------------------
# Mocked DB
# ---------------------------------------------------------------------------


class _FakeTenant:
    def __init__(self, id: str, slug: str | None = None) -> None:
        self.id = id
        self.slug = slug
        self.name = "Default Tenant"


class _FakeUser:
    def __init__(self, id: str, tenant_id: str, name: str, email=None, role="member") -> None:
        self.id = id
        self.tenant_id = tenant_id
        self.name = name
        self.email = email
        self.role = role
        self.external_sub: str | None = None


@pytest.fixture
def mock_db():
    state = {
        "users": {},
        "tenants": {"default": _FakeTenant("default-tenant", slug="default")},
        "tenant_map": {},
    }

    async def get_tenant_by_slug(slug):
        return state["tenants"].get(slug)

    async def create_tenant(name, slug=None, **_):
        t = _FakeTenant(f"tenant-{len(state['tenants'])}", slug=slug)
        if slug:
            state["tenants"][slug] = t
        return t

    async def get_local_tenant_id(provider, ext):
        return state["tenant_map"].get((provider, ext))

    async def create_external_tenant_mapping(provider, ext, local):
        state["tenant_map"][(provider, ext)] = local

    async def get_user_by_external_sub(sub):
        return state["users"].get(sub)

    async def create_user_with_external_sub(tenant_id, name, email, role, external_sub):
        u = _FakeUser(f"user-{len(state['users'])}", tenant_id, name, email, role)
        u.external_sub = external_sub
        state["users"][external_sub] = u
        return u

    async def update_user(user_id, **fields):
        for u in state["users"].values():
            if u.id == user_id:
                for k, v in fields.items():
                    if v is not None:
                        setattr(u, k, v)
                return u
        return None

    with (
        patch("rolemesh.db.get_tenant_by_slug", AsyncMock(side_effect=get_tenant_by_slug)),
        patch("rolemesh.db.create_tenant", AsyncMock(side_effect=create_tenant)),
        patch("rolemesh.db.get_local_tenant_id", AsyncMock(side_effect=get_local_tenant_id)),
        patch(
            "rolemesh.db.create_external_tenant_mapping",
            AsyncMock(side_effect=create_external_tenant_mapping),
        ),
        patch(
            "rolemesh.db.get_user_by_external_sub",
            AsyncMock(side_effect=get_user_by_external_sub),
        ),
        patch(
            "rolemesh.db.create_user_with_external_sub",
            AsyncMock(side_effect=create_user_with_external_sub),
        ),
        patch("rolemesh.db.update_user", AsyncMock(side_effect=update_user)),
    ):
        yield state


# ---------------------------------------------------------------------------
# OIDC env + provider setup
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_oidc(monkeypatch, mock_httpx, mock_db):
    monkeypatch.setenv(
        "OIDC_DISCOVERY_URL", "https://test.example.com/.well-known/openid-configuration"
    )
    monkeypatch.setenv("OIDC_CLIENT_ID", "test-client")
    monkeypatch.setenv("OIDC_AUDIENCE", "test-client")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "http://localhost:8080/oauth2/callback")
    monkeypatch.setenv("OIDC_COOKIE_SECURE", "false")  # allow http in tests

    import rolemesh.auth.oidc.config
    import webui.config

    importlib.reload(rolemesh.auth.oidc.config)
    importlib.reload(webui.config)

    # Set up provider in webui.auth module
    from rolemesh.auth.oidc.adapter import DefaultOIDCAdapter
    from rolemesh.auth.oidc.config import OIDCConfig
    from rolemesh.auth.oidc.provider import OIDCAuthProvider
    from webui import auth

    auth._provider = OIDCAuthProvider(
        OIDCConfig(
            discovery_url="https://test.example.com/.well-known/openid-configuration",
            client_id="test-client",
            audience="test-client",
        ),
        adapter=DefaultOIDCAdapter(),
    )

    # Mount router on a fresh FastAPI app for testing
    import webui.oidc_routes

    importlib.reload(webui.oidc_routes)
    from webui.oidc_routes import router

    app = FastAPI()
    app.include_router(router)

    yield app, mock_httpx

    auth._provider = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_exchange_sets_refresh_cookie(app_with_oidc, make_token):
    app, httpx_state = app_with_oidc
    id_token = make_token(
        {
            "sub": "user-1",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Alice",
        }
    )
    httpx_state["post_routes"]["https://test.example.com/token"].append(
        (200, {"id_token": id_token, "refresh_token": "rt-1", "expires_in": 3600})
    )

    client = TestClient(app)
    resp = client.post(
        "/api/auth/exchange",
        json={"code": "auth-code", "code_verifier": "verifier"},
    )
    assert resp.status_code == 200
    assert resp.json()["id_token"] == id_token
    # Cookie should be set
    assert "rm_refresh" in resp.cookies
    assert resp.cookies["rm_refresh"] == "rt-1"


def test_refresh_with_cookie(app_with_oidc, make_token):
    app, httpx_state = app_with_oidc
    new_id_token = make_token(
        {
            "sub": "user-1",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Alice",
        }
    )
    # IdP returns same refresh_token (no rotation)
    httpx_state["post_routes"]["https://test.example.com/token"].append(
        (200, {"id_token": new_id_token, "refresh_token": "rt-1", "expires_in": 3600})
    )

    client = TestClient(app)
    client.cookies.set("rm_refresh", "rt-1", path="/api/auth")
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200
    assert resp.json()["id_token"] == new_id_token
    # Cookie not changed (no rotation), so no Set-Cookie expected
    # But the test client may still have the cookie present
    assert "rm_refresh" in client.cookies
    assert client.cookies["rm_refresh"] == "rt-1"


def test_refresh_with_rotation(app_with_oidc, make_token):
    app, httpx_state = app_with_oidc
    new_id_token = make_token(
        {
            "sub": "user-1",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Alice",
        }
    )
    # IdP rotates refresh_token: returns rt-2
    httpx_state["post_routes"]["https://test.example.com/token"].append(
        (200, {"id_token": new_id_token, "refresh_token": "rt-2", "expires_in": 3600})
    )

    client = TestClient(app)
    client.cookies.set("rm_refresh", "rt-1", path="/api/auth")
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200
    # New refresh_token should be in the response Set-Cookie header
    set_cookie = resp.headers.get("set-cookie", "")
    assert "rm_refresh=rt-2" in set_cookie


def test_refresh_without_cookie(app_with_oidc):
    app, _ = app_with_oidc
    client = TestClient(app)
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401


def test_refresh_idp_rejects(app_with_oidc):
    app, httpx_state = app_with_oidc
    httpx_state["post_routes"]["https://test.example.com/token"].append(
        (400, {"error": "invalid_grant"})
    )

    client = TestClient(app)
    client.cookies.set("rm_refresh", "rt-revoked", path="/api/auth")
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 401
    # Cookie should be cleared (max-age=0 or similar)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "rm_refresh" in set_cookie  # delete_cookie sends Set-Cookie with empty value


def test_logout_clears_cookie(app_with_oidc, make_token):
    app, _ = app_with_oidc
    client = TestClient(app)
    client.cookies.set("rm_refresh", "rt-1", path="/api/auth")
    # Logout requires a valid id_token in Authorization header (CSRF mitigation)
    id_token = make_token(
        {
            "sub": "user-logout",
            "iss": "https://test.example.com/",
            "aud": "test-client",
            "exp": 9999999999,
            "iat": 1000000000,
            "name": "Logout User",
        }
    )
    resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {id_token}"})
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "rm_refresh" in set_cookie


def test_logout_without_auth_rejected(app_with_oidc):
    app, _ = app_with_oidc
    client = TestClient(app)
    client.cookies.set("rm_refresh", "rt-1", path="/api/auth")
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 401
