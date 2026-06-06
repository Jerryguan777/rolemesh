"""Tests for the admin-provisioning core (CLI + env-seed share it).

Contract under test (task §6 acceptance):
  - ``create_admin`` on an empty DB creates one platform_admin; a repeat
    run is idempotent (no error, same user_id, created=False).
  - ``--role owner --tenant <slug>`` creates a tenant-scoped row as the
    escape hatch; tenant-scoped roles require a tenant.
  - ``ensure_seed_admin`` is off unless ROLEMESH_SEED_ADMIN_EMAIL is set,
    seeds a platform_admin when set, and skips an existing one.
  - The write uses the BYPASSRLS admin pool — no auth context needed.
"""

from __future__ import annotations

import argparse

import pytest

from rolemesh.admin.cli import _build_parser, _cmd_create_admin
from rolemesh.admin.core import (
    PLATFORM_ADMIN_ROLE,
    PLATFORM_TENANT_SLUG,
    AdminProvisionError,
    create_admin,
    ensure_seed_admin,
)
from rolemesh.db import get_tenant_by_slug
from rolemesh.db._pool import admin_conn

pytestmark = pytest.mark.usefixtures("test_db")


async def _user_count_by_email(email: str) -> int:
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM users WHERE email = $1", email
        )
    assert row is not None
    return int(row["n"])


# ---------------------------------------------------------------------------
# create_admin — platform genesis (default role)
# ---------------------------------------------------------------------------


async def test_creates_platform_admin_in_sentinel_tenant() -> None:
    result = await create_admin(email="a@b.com")

    assert result.created is True
    assert result.role == PLATFORM_ADMIN_ROLE
    assert result.tenant_slug == PLATFORM_TENANT_SLUG
    assert result.email == "a@b.com"
    sentinel = await get_tenant_by_slug(PLATFORM_TENANT_SLUG)
    assert sentinel is not None
    assert result.tenant_id == sentinel.id
    assert await _user_count_by_email("a@b.com") == 1


async def test_sentinel_tenant_created_once_and_reused() -> None:
    """Provisioning two platform_admins must not duplicate the sentinel tenant.

    Regression guard: the anchor tenant is created idempotently. A second
    platform_admin (distinct email) must land in the SAME ``__platform__``
    tenant row, not a fresh one.
    """
    first = await create_admin(email="one@b.com")
    second = await create_admin(email="two@b.com")

    assert first.tenant_id == second.tenant_id
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM tenants WHERE slug = $1",
            PLATFORM_TENANT_SLUG,
        )
    assert row is not None
    assert int(row["n"]) == 1


async def test_create_admin_is_idempotent() -> None:
    first = await create_admin(email="a@b.com")
    second = await create_admin(email="a@b.com")

    assert first.created is True
    assert second.created is False
    assert second.user_id == first.user_id
    # The duplicate run did not write a second row.
    assert await _user_count_by_email("a@b.com") == 1


async def test_name_defaults_to_email_local_part() -> None:
    result = await create_admin(email="alice@example.com")
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM users WHERE id = $1::uuid", result.user_id
        )
    assert row is not None
    assert row["name"] == "alice"


async def test_explicit_name_is_used() -> None:
    result = await create_admin(email="alice@example.com", name="Alice A")
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM users WHERE id = $1::uuid", result.user_id
        )
    assert row is not None
    assert row["name"] == "Alice A"


# ---------------------------------------------------------------------------
# external_sub binding + idempotency by sub
# ---------------------------------------------------------------------------


async def test_external_sub_is_bound_and_idempotent_by_sub() -> None:
    first = await create_admin(email="a@b.com", external_sub="idp|123")
    assert first.created is True
    assert first.external_sub == "idp|123"

    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT external_sub FROM users WHERE id = $1::uuid", first.user_id
        )
    assert row is not None
    assert row["external_sub"] == "idp|123"

    # A different email but the same sub resolves to the same row.
    second = await create_admin(email="other@b.com", external_sub="idp|123")
    assert second.created is False
    assert second.user_id == first.user_id


# ---------------------------------------------------------------------------
# Role validation + tenant-scoped escape hatch
# ---------------------------------------------------------------------------


async def test_rejects_unknown_role() -> None:
    with pytest.raises(AdminProvisionError, match="role"):
        await create_admin(email="a@b.com", role="godmode")


async def test_tenant_scoped_role_requires_tenant() -> None:
    with pytest.raises(AdminProvisionError, match="tenant"):
        await create_admin(email="a@b.com", role="owner")


async def test_platform_admin_rejects_foreign_tenant_flag() -> None:
    with pytest.raises(AdminProvisionError, match="platform-scoped"):
        await create_admin(email="a@b.com", tenant="acme")


async def test_owner_with_tenant_creates_tenant_and_user() -> None:
    result = await create_admin(email="boss@acme.com", role="owner", tenant="acme")

    assert result.created is True
    assert result.role == "owner"
    assert result.tenant_slug == "acme"
    # The tenant was auto-created.
    acme = await get_tenant_by_slug("acme")
    assert acme is not None
    assert result.tenant_id == acme.id


# ---------------------------------------------------------------------------
# ensure_seed_admin — env wrapper
# ---------------------------------------------------------------------------


async def test_seed_admin_off_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROLEMESH_SEED_ADMIN_EMAIL", raising=False)
    assert await ensure_seed_admin() is None


async def test_seed_admin_seeds_platform_admin_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROLEMESH_SEED_ADMIN_EMAIL", "seed@b.com")
    monkeypatch.setenv("ROLEMESH_SEED_ADMIN_NAME", "Seed Admin")

    result = await ensure_seed_admin()
    assert result is not None
    assert result.created is True
    assert result.role == PLATFORM_ADMIN_ROLE

    # Idempotent: a second startup leaves it untouched.
    again = await ensure_seed_admin()
    assert again is not None
    assert again.created is False
    assert again.user_id == result.user_id
    assert await _user_count_by_email("seed@b.com") == 1


async def test_seed_admin_uses_same_core_as_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CLI create then the seed of the same email must converge on the
    # one row — proving both go through the shared core.
    cli = await create_admin(email="shared@b.com")
    monkeypatch.setenv("ROLEMESH_SEED_ADMIN_EMAIL", "shared@b.com")
    seed = await ensure_seed_admin()
    assert seed is not None
    assert seed.created is False
    assert seed.user_id == cli.user_id


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_cli_parser_defaults_to_platform_admin() -> None:
    args = _build_parser().parse_args(["create-admin", "--email", "a@b.com"])
    assert args.command == "create-admin"
    assert args.email == "a@b.com"
    assert args.role == PLATFORM_ADMIN_ROLE
    assert args.tenant is None


async def test_cli_command_creates_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI manages its own pools via init_database/close_database; the
    # test_db fixture already opened pools on this event loop, so stub
    # those out and exercise the command against the live test DB.
    import rolemesh.db as db_pkg

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(db_pkg, "init_database", _noop)
    monkeypatch.setattr(db_pkg, "close_database", _noop)

    args = argparse.Namespace(
        command="create-admin",
        email="cli@b.com",
        role=PLATFORM_ADMIN_ROLE,
        external_sub=None,
        name=None,
        tenant=None,
    )
    rc = await _cmd_create_admin(args)
    assert rc == 0
    assert await _user_count_by_email("cli@b.com") == 1
