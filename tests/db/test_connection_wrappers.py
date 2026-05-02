"""PR-B: tenant_conn / admin_conn behaviour.

These tests pin the connection-wrapper contract:

  - tenant_conn(tenant_id) sets app.current_tenant_id only inside
    the wrapper's transaction; the GUC must clear when the wrapper
    exits.
  - Reusing the same physical connection across acquires (which
    asyncpg's pool does) must not leak the previous tenant's GUC.
  - admin_conn() never sets the GUC.
  - The roles ``rolemesh_app`` and ``rolemesh_system`` exist after
    schema bootstrap. (We can't easily flip the test container's
    DSN to log in *as* those roles, so this is a sanity check that
    PR-B's CREATE ROLE block ran.)
"""

from __future__ import annotations

import pytest

from rolemesh.db import (
    _get_admin_pool,
    _get_pool,
    admin_conn,
    tenant_conn,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def test_tenant_conn_sets_current_tenant_id() -> None:
    """Inside the wrapper, current_tenant_id() returns the value we set."""
    tid = "11111111-1111-1111-1111-111111111111"
    async with tenant_conn(tid) as conn:
        observed = await conn.fetchval("SELECT current_tenant_id()::text")
    assert observed == tid


async def test_tenant_conn_clears_guc_on_exit() -> None:
    """After the wrapper exits, the next acquire on the same pool must
    NOT see the previous tenant's GUC. This is the regression that
    happens if someone forgets to wrap set_config inside a transaction
    (with is_local=true)."""
    tid = "22222222-2222-2222-2222-222222222222"
    async with tenant_conn(tid) as conn:
        assert await conn.fetchval("SELECT current_tenant_id()::text") == tid
    # Bypass the wrapper and read the GUC directly. asyncpg's pool may
    # or may not hand us the same physical connection back, but either
    # way the answer must be NULL — both because the connection was
    # transactionally scoped (is_local=true) AND because RESET ALL is
    # called between holders by asyncpg.
    pool = _get_pool()
    async with pool.acquire() as conn:
        observed = await conn.fetchval("SELECT current_tenant_id()::text")
    assert observed is None


async def test_tenant_conn_back_to_back_no_residue() -> None:
    """Two tenant_conn calls in sequence must each see only their own
    tenant — A's GUC must not bleed into B's wrapper."""
    a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    async with tenant_conn(a) as conn:
        assert await conn.fetchval("SELECT current_tenant_id()::text") == a
    async with tenant_conn(b) as conn:
        assert await conn.fetchval("SELECT current_tenant_id()::text") == b


async def test_admin_conn_has_null_current_tenant_id() -> None:
    """admin_conn never sets the GUC; current_tenant_id() returns NULL.
    Once RLS lands in PR-D, this is what makes the role's BYPASSRLS
    bit (or absence of policies) the *only* gate on cross-tenant
    reads — there's no way to set the GUC accidentally."""
    async with admin_conn() as conn:
        observed = await conn.fetchval("SELECT current_tenant_id()::text")
    assert observed is None


async def test_rls_roles_were_created() -> None:
    """PR-B's _create_schema must have provisioned both roles. If this
    fails after a clean DB init, the DO $$ ... CREATE ROLE block
    silently failed (e.g. bootstrap user lacks CREATEROLE)."""
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        names = await conn.fetch(
            "SELECT rolname FROM pg_roles WHERE rolname IN "
            "('rolemesh_app', 'rolemesh_system') ORDER BY rolname"
        )
    assert [r["rolname"] for r in names] == ["rolemesh_app", "rolemesh_system"]


async def test_rls_function_returns_null_when_unset() -> None:
    """current_tenant_id() must fail closed (return NULL, not error)
    when the GUC isn't set — required so RLS policies treat the row
    as excluded rather than crashing the query."""
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        observed = await conn.fetchval("SELECT current_tenant_id()")
    assert observed is None
