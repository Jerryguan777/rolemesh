"""Tests for credential validation — the probe and its HTTP surface.

Two layers:

* Unit tests on :func:`webui.v1.credential_probe.probe_credential` with a
  faked ``httpx.AsyncClient`` — they pin the per-provider routing (URL +
  auth header reused from the egress reverse proxy) and the status →
  verdict classification, with no network.
* An endpoint test on ``POST /api/v1/credentials/{provider}/validate``
  that pins the wire contract: a bad key is a 200 with ``ok=false`` (not a
  4xx), and the plaintext never appears in the response.
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
from webui.v1 import credential_probe
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Faked httpx — capture the request, return a status or raise.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Stands in for ``httpx.AsyncClient`` as an async context manager.

    Records every ``get`` into a shared list so a test can assert which
    upstream + headers the probe dialed. Returns ``status`` or raises
    ``exc`` (a transport-level ``httpx.HTTPError``).
    """

    def __init__(self, calls: list, *, status: int | None, exc: Exception | None):
        self._calls = calls
        self._status = status
        self._exc = exc

    def __call__(self, *_a, **_k):  # construction: httpx.AsyncClient(timeout=...)
        return self

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def get(self, url: str, headers: dict | None = None):
        self._calls.append((url, headers or {}))
        if self._exc is not None:
            raise self._exc
        assert self._status is not None
        return _FakeResp(self._status)


@pytest.fixture
def fake_http(monkeypatch):
    """Install a fake AsyncClient; returns a configure(status/exc) -> calls fn."""
    calls: list = []

    def configure(*, status: int | None = None, exc: Exception | None = None):
        monkeypatch.setattr(
            credential_probe.httpx,
            "AsyncClient",
            _FakeClient(calls, status=status, exc=exc),
        )
        return calls

    return configure


# ---------------------------------------------------------------------------
# Probe unit tests
# ---------------------------------------------------------------------------


async def test_anthropic_valid_key_hits_models_with_version(fake_http):
    calls = fake_http(status=200)
    r = await credential_probe.probe_credential("anthropic", {"api_key": "sk-ant-x"})
    assert r.ok is True
    assert r.level == "verified"
    url, headers = calls[0]
    assert url.endswith("/v1/models")
    assert headers["x-api-key"] == "sk-ant-x"
    # /v1/models 400s without the version header — the probe must send it.
    assert headers["anthropic-version"]


async def test_anthropic_oauth_token_extras_shape(fake_http):
    calls = fake_http(status=200)
    r = await credential_probe.probe_credential(
        "anthropic", {"api_key": "", "extras": {"oauth_token": "oauth-abc"}},
    )
    assert r.ok is True
    _url, headers = calls[0]
    assert headers["authorization"] == "Bearer oauth-abc"


async def test_anthropic_401_is_rejected_not_crashed(fake_http):
    fake_http(status=401)
    r = await credential_probe.probe_credential("anthropic", {"api_key": "bad"})
    assert r.ok is False
    assert r.level == "verified"
    assert "401" in r.detail


async def test_403_reports_insufficient_permission(fake_http):
    fake_http(status=403)
    r = await credential_probe.probe_credential("openai", {"api_key": "k"})
    assert r.ok is False
    assert "403" in r.detail


async def test_429_counts_as_live_key(fake_http):
    fake_http(status=200 if False else 429)
    r = await credential_probe.probe_credential("openai", {"api_key": "k"})
    # Rate-limited still proves the key authenticated.
    assert r.ok is True
    assert r.level == "verified"


async def test_openai_dials_v1_models_with_bearer(fake_http):
    calls = fake_http(status=200)
    await credential_probe.probe_credential("openai", {"api_key": "sk-oai"})
    url, headers = calls[0]
    assert url.endswith("/v1/models")
    assert headers["authorization"] == "Bearer sk-oai"


async def test_google_dials_v1beta_models_with_api_key_header(fake_http):
    calls = fake_http(status=200)
    await credential_probe.probe_credential("google", {"api_key": "g-key"})
    url, headers = calls[0]
    assert url.endswith("/v1beta/models")
    assert headers["x-goog-api-key"] == "g-key"


async def test_missing_key_short_circuits_without_network(fake_http):
    calls = fake_http(status=200)
    r = await credential_probe.probe_credential("openai", {"api_key": ""})
    assert r.ok is False
    assert calls == []  # never dialed


async def test_timeout_is_reachable_failure(fake_http):
    fake_http(exc=httpx.TimeoutException("slow"))
    r = await credential_probe.probe_credential("anthropic", {"api_key": "k"})
    assert r.ok is False
    assert r.level == "reachable"
    assert "imed out" in r.detail


async def test_connect_error_is_reachable_failure(fake_http):
    fake_http(exc=httpx.ConnectError("no route"))
    r = await credential_probe.probe_credential("openai", {"api_key": "k"})
    assert r.ok is False
    assert r.level == "reachable"


async def test_bedrock_reachable_includes_region_and_is_not_verified(fake_http):
    calls = fake_http(status=403)  # AWS error still proves reachability
    r = await credential_probe.probe_credential(
        "bedrock", {"api_key": "k", "extras": {"region": "eu-west-1"}},
    )
    assert r.ok is True
    assert r.level == "reachable"
    assert "eu-west-1" in calls[0][0]


async def test_bedrock_unreachable_region(fake_http):
    fake_http(exc=httpx.ConnectError("nxdomain"))
    r = await credential_probe.probe_credential(
        "bedrock", {"api_key": "k", "extras": {"region": "bogus"}},
    )
    assert r.ok is False
    assert r.level == "reachable"


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


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
    t = await create_tenant(name="T", slug=f"v-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="A", email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return AuthenticatedUser(
        user_id=u.id, tenant_id=t.id, role="owner", email="x@x.com", name="X",
    )


async def test_validate_endpoint_bad_key_is_200_not_4xx(fake_http):
    fake_http(status=401)
    user = await _make_user()
    sentinel = f"SENTINEL_{uuid.uuid4().hex}"
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/credentials/anthropic/validate",
            json={"api_key": sentinel},
            headers={"Authorization": "Bearer x"},
        )
    # A rejected key is a successful *test*, not a transport error.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["provider"] == "anthropic"
    assert body["level"] == "verified"
    # The plaintext key must never echo back in the verdict.
    assert sentinel not in resp.text


async def test_validate_endpoint_good_key_ok_true(fake_http):
    fake_http(status=200)
    user = await _make_user()
    async with _client(_build_app(user)) as ac:
        resp = await ac.post(
            "/api/v1/credentials/openai/validate",
            json={"api_key": "sk-good"},
            headers={"Authorization": "Bearer x"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
