"""ROLEMESH_ENV=production hardening of the legacy bootstrap paths.

  - ADMIN_BOOTSTRAP_TOKEN no longer authorizes in production (fail
    closed) but still works in development (default).
  - BOOTSTRAP_USERS aborts startup in production but only warns / works
    in development.

The default (development) behaviour must be unchanged so the existing
ADMIN_BOOTSTRAP_TOKEN-dependent suite is not disturbed.
"""

from __future__ import annotations

import json

import pytest

from rolemesh.auth.bootstrap_users import (
    BootstrapUsersConfigError,
    _reset_for_tests,
    init_bootstrap_users,
)

pytestmark = pytest.mark.usefixtures("test_db")


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


async def _ensure_default_tenant() -> str:
    from rolemesh.db import create_tenant, get_tenant_by_slug

    existing = await get_tenant_by_slug("default")
    if existing is not None:
        return existing.id
    t = await create_tenant(name="default", slug="default")
    return t.id


# ---------------------------------------------------------------------------
# ADMIN_BOOTSTRAP_TOKEN
# ---------------------------------------------------------------------------


async def test_bootstrap_token_authorizes_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_default_tenant()
    init_bootstrap_users("")
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "legacy-tok")
    monkeypatch.setattr("webui.config.IS_PRODUCTION", False)

    from webui import auth as webui_auth

    user = await webui_auth.authenticate_ws("legacy-tok")
    assert user is not None
    assert user.role == "owner"


async def test_bootstrap_token_fails_closed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_default_tenant()
    init_bootstrap_users("")
    monkeypatch.setattr("webui.config.ADMIN_BOOTSTRAP_TOKEN", "legacy-tok")
    monkeypatch.setattr("webui.config.IS_PRODUCTION", True)

    from webui import auth as webui_auth

    # The token branch must not authorize; with no provider configured
    # the request lands unauthenticated.
    assert await webui_auth.authenticate_ws("legacy-tok") is None


# ---------------------------------------------------------------------------
# BOOTSTRAP_USERS
# ---------------------------------------------------------------------------


def _one_spec() -> str:
    return json.dumps(
        [{"token": "tok-a", "user_id": "a", "tenant": "default", "role": "owner"}]
    )


def test_bootstrap_users_hard_fails_in_production() -> None:
    with pytest.raises(BootstrapUsersConfigError, match="production"):
        init_bootstrap_users(_one_spec(), rolemesh_env="production")


def test_bootstrap_users_ok_in_development() -> None:
    # Does not raise; the map is parsed for the dev/test fast-path.
    init_bootstrap_users(_one_spec(), rolemesh_env="development")


def test_empty_bootstrap_users_ok_in_production() -> None:
    # The feature being off is always fine, even in production.
    init_bootstrap_users("", rolemesh_env="production")
