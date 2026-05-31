"""PR5 pinned test: BOOTSTRAP_USERS multi-user map.

Covers the five scenarios spelled out in session 00a:
  1. Single-token legacy path (ADMIN_BOOTSTRAP_TOKEN) is unchanged.
  2. tok-alice → user row inserted, returned UUID is the stable
     ``uuid5`` of the slug.
  3. tok-bob → second row inserted; a repeat call for tok-alice does
     NOT re-INSERT (ON CONFLICT DO NOTHING + the in-process upsert
     cache).
  4. Malformed BOOTSTRAP_USERS spec → init_bootstrap_users raises.
  5. Token not in the map and not equal to ADMIN_BOOTSTRAP_TOKEN
     falls through to the provider (we exercise this by setting the
     provider to a stub that records the call).

Anti-mirror: the test imports the parse/init API on top, but the
behaviour assertions (returned UUID matches uuid5, DB row count is
exactly 1 after dup hits, role mapping is preserved) describe the
contract from outside, not the implementation.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from rolemesh.auth.bootstrap_users import (
    BootstrapUsersConfigError,
    _reset_for_tests,
    init_bootstrap_users,
    parse_bootstrap_users_env,
)
from rolemesh.db import create_tenant
from rolemesh.db._pool import admin_conn

pytestmark = pytest.mark.usefixtures("test_db")


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


async def _ensure_default_tenant() -> str:
    from rolemesh.db import get_tenant_by_slug

    existing = await get_tenant_by_slug("default")
    if existing is not None:
        return existing.id
    t = await create_tenant(name="default", slug="default")
    return t.id


# ---------------------------------------------------------------------------
# Parser: invalid specs must fail loud
# ---------------------------------------------------------------------------


def test_parse_rejects_non_list() -> None:
    with pytest.raises(BootstrapUsersConfigError):
        parse_bootstrap_users_env('{"token": "x"}')


def test_parse_rejects_missing_field() -> None:
    payload = json.dumps([{"token": "tok-a", "user_id": "a", "tenant": "t"}])
    with pytest.raises(BootstrapUsersConfigError, match="missing"):
        parse_bootstrap_users_env(payload)


def test_parse_rejects_unknown_role() -> None:
    payload = json.dumps(
        [{"token": "tok-a", "user_id": "a", "tenant": "t", "role": "godmode"}]
    )
    with pytest.raises(BootstrapUsersConfigError, match="role"):
        parse_bootstrap_users_env(payload)


def test_parse_rejects_duplicate_tokens() -> None:
    payload = json.dumps(
        [
            {"token": "same", "user_id": "a", "tenant": "t", "role": "owner"},
            {"token": "same", "user_id": "b", "tenant": "t", "role": "member"},
        ]
    )
    with pytest.raises(BootstrapUsersConfigError, match="duplicate"):
        parse_bootstrap_users_env(payload)


def test_parse_empty_env_yields_empty_map() -> None:
    assert parse_bootstrap_users_env(None) == {}
    assert parse_bootstrap_users_env("") == {}


def test_init_raises_on_malformed_env() -> None:
    with pytest.raises(BootstrapUsersConfigError):
        init_bootstrap_users('{"not": "a list"}')


# ---------------------------------------------------------------------------
# End-to-end through ``authenticate_ws`` against the real DB
# ---------------------------------------------------------------------------


async def _user_count_for_tenant(tenant_id: str) -> int:
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM users WHERE tenant_id = $1::uuid",
            tenant_id,
        )
    assert row is not None
    return int(row["n"])


async def test_multi_user_first_hit_inserts_row_and_returns_stable_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _ensure_default_tenant()
    env = json.dumps(
        [
            {"token": "tok-alice", "user_id": "alice", "tenant": "default", "role": "owner"},
        ]
    )
    monkeypatch.setenv("BOOTSTRAP_USERS", env)
    init_bootstrap_users(env)
    # Make sure no legacy ADMIN_BOOTSTRAP_TOKEN interferes.
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "")

    from webui import auth as webui_auth

    user = await webui_auth.authenticate_ws("tok-alice")
    assert user is not None
    assert user.tenant_id == tenant_id
    assert user.role == "owner"
    expected_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "bootstrap:alice"))
    assert user.user_id == expected_uuid
    # Row landed in the DB.
    assert await _user_count_for_tenant(tenant_id) == 1


async def test_multi_user_two_users_each_get_their_own_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _ensure_default_tenant()
    env = json.dumps(
        [
            {"token": "tok-alice", "user_id": "alice", "tenant": "default", "role": "owner"},
            {"token": "tok-bob", "user_id": "bob", "tenant": "default", "role": "member"},
        ]
    )
    init_bootstrap_users(env)
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "")

    from webui import auth as webui_auth

    a1 = await webui_auth.authenticate_ws("tok-alice")
    b = await webui_auth.authenticate_ws("tok-bob")
    a2 = await webui_auth.authenticate_ws("tok-alice")  # repeat
    assert a1 is not None and b is not None and a2 is not None
    assert a1.user_id != b.user_id
    assert a1.user_id == a2.user_id  # stable, idempotent
    assert b.role == "member"
    # Two rows total — the duplicate Alice hit is a no-op.
    assert await _user_count_for_tenant(tenant_id) == 2


async def test_legacy_admin_bootstrap_token_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_default_tenant()
    # Disable multi-user; keep the legacy token.
    init_bootstrap_users("")
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "legacy-tok")

    from webui import auth as webui_auth

    user = await webui_auth.authenticate_ws("legacy-tok")
    assert user is not None
    # The legacy path still returns the literal "bootstrap" — that's
    # what makes the resolve_actor_user_id helper from PR4 necessary.
    assert user.user_id == "bootstrap"
    assert user.role == "owner"


async def test_unknown_token_falls_through_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_default_tenant()
    env = json.dumps(
        [{"token": "tok-alice", "user_id": "alice", "tenant": "default", "role": "owner"}]
    )
    init_bootstrap_users(env)
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "")

    from webui import auth as webui_auth

    called_with: list[str] = []

    async def stub_authenticate_request(token: str) -> Any:
        called_with.append(token)
        return None

    monkeypatch.setattr(webui_auth, "authenticate_request", stub_authenticate_request)

    out = await webui_auth.authenticate_ws("totally-unknown-token")
    assert out is None
    assert called_with == ["totally-unknown-token"]


async def test_multi_user_spec_pointing_at_missing_tenant_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No tenant created — spec references a nonexistent slug. The
    # resolver must NOT silently invent a tenant_id; return None so
    # the request lands on 401.
    env = json.dumps(
        [
            {"token": "tok-ghost", "user_id": "ghost", "tenant": "no-such-tenant", "role": "member"},
        ]
    )
    init_bootstrap_users(env)
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "")

    from webui import auth as webui_auth

    out = await webui_auth.authenticate_ws("tok-ghost")
    assert out is None
