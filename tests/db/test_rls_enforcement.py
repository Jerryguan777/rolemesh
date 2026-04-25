"""PR-E: end-to-end RLS enforcement tests.

These run AFTER PR-D enabled policies on every tenant table. The
goal is to verify that the policies actually block cross-tenant
access at the DB layer — not just at the application WHERE clause.

Tests use a dedicated asyncpg pool that connects as
``rolemesh_app`` (NOBYPASSRLS). The default test pool connects
as the testcontainer's superuser, which bypasses RLS regardless of
ENABLE/FORCE — useful for fixture setup but useless for verifying
the policy itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest

from rolemesh.db import pg
from rolemesh.db.pg import (
    _get_pool,
    admin_conn,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    tenant_conn,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# rolemesh_app pool — NOBYPASSRLS, the only role that actually feels RLS
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_pool(pg_url: str) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record], None]:
    """Spin up a separate pool that logs in as ``rolemesh_app``.

    The role was provisioned by PR-B's _create_schema; we set its
    password here (in-memory testcontainer, throwaway value) so we
    can connect with a normal DSN.
    """
    superuser_pool = _get_pool()
    async with superuser_pool.acquire() as conn:
        await conn.execute("ALTER USER rolemesh_app PASSWORD 'test'")

    # Rewrite the DSN to log in as rolemesh_app — testcontainer DSN
    # has the form postgresql://test:test@host:port/db; swap creds.
    rewritten = pg_url.replace("test:test@", "rolemesh_app:test@", 1)
    pool = await asyncpg.create_pool(rewritten, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


async def _two_tenants_full() -> dict[str, dict[str, str]]:
    """Build two complete chains so we have approval_requests to query."""
    out: dict[str, dict[str, str]] = {}
    for tag in ("A", "B"):
        t = await create_tenant(name=f"T{tag}", slug=f"rls-{tag.lower()}-{uuid.uuid4().hex[:6]}")
        u = await create_user(
            tenant_id=t.id, name=f"U{tag}",
            email=f"u-{tag.lower()}-{uuid.uuid4().hex[:6]}@x.com",
            role="owner",
        )
        cw = await create_coworker(
            tenant_id=t.id, name=f"CW{tag}",
            folder=f"cw-{tag.lower()}-{uuid.uuid4().hex[:6]}"
        )
        b = await create_channel_binding(
            coworker_id=cw.id, tenant_id=t.id,
            channel_type="telegram", credentials={"bot_token": "x"},
        )
        conv = await create_conversation(
            tenant_id=t.id, coworker_id=cw.id, channel_binding_id=b.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        req = await create_approval_request(
            tenant_id=t.id, coworker_id=cw.id, conversation_id=conv.id,
            policy_id=None, user_id=u.id, job_id=f"j-{uuid.uuid4().hex[:8]}",
            mcp_server_name="erp",
            actions=[{"tool_name": "refund", "params": {}}],
            action_hashes=[uuid.uuid4().hex],
            rationale="t", source="proposal", status="pending",
            resolved_approvers=[u.id],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        out[tag] = {
            "tenant_id": t.id,
            "user_id": u.id,
            "coworker_id": cw.id,
            "request_id": req.id,
        }
    return out


# ---------------------------------------------------------------------------
# Cross-tenant blocking (the main reason RLS exists)
# ---------------------------------------------------------------------------


async def test_app_role_select_blocked_across_tenant(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Set the GUC to tenant A, then issue a SELECT that explicitly
    asks for tenant B's rows. RLS must drop them — even though the
    tenant_id literal in the WHERE clause matches a real row, the
    policy's ``USING (tenant_id = current_tenant_id())`` adds an
    AND that filters it back out."""
    tenants = await _two_tenants_full()
    a, b = tenants["A"], tenants["B"]

    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                a["tenant_id"],
            )
            rows = await conn.fetch(
                "SELECT * FROM approval_requests WHERE tenant_id = $1::uuid",
                b["tenant_id"],
            )
    assert rows == [], (
        "RLS failed: rolemesh_app saw tenant B rows while bound to tenant A"
    )


async def test_app_role_insert_blocked_across_tenant(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Try to INSERT a row whose tenant_id != current_tenant_id().
    The policy's ``WITH CHECK`` clause must reject it. asyncpg
    surfaces this as InsufficientPrivilegeError (PG SQLSTATE 42501)
    or CheckViolation depending on PG's classification."""
    tenants = await _two_tenants_full()
    a, b = tenants["A"], tenants["B"]
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                a["tenant_id"],
            )
            with pytest.raises(
                (asyncpg.InsufficientPrivilegeError, asyncpg.CheckViolationError)
            ):
                await conn.execute(
                    "INSERT INTO approval_audit_log "
                    "(request_id, tenant_id, action) "
                    "VALUES ($1::uuid, $2::uuid, $3)",
                    a["request_id"],  # valid request id, but...
                    b["tenant_id"],   # ...wrong tenant_id
                    "noop",
                )


async def test_unset_guc_returns_empty(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Forgot to call set_config? Every tenant table must look empty
    rather than returning all-rows-because-NULL-is-falsy. This is
    the fail-closed contract that current_tenant_id()'s NULLIF
    enforces."""
    await _two_tenants_full()
    async with app_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM approval_requests")
    assert rows == [], (
        "RLS fail-closed broken: unset GUC returned rows from approval_requests"
    )


async def test_admin_pool_bypasses_rls() -> None:
    """admin_conn() runs as the superuser (in test) or rolemesh_system
    (in prod). Either way it has BYPASSRLS, so cross-tenant queries
    work — that's the whole point of this pool. Without this carve-out
    the maintenance loops can't reconcile across tenants."""
    await _two_tenants_full()
    async with admin_conn() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT tenant_id FROM approval_requests"
        )
    assert len({str(r["tenant_id"]) for r in rows}) >= 2, (
        "admin_conn should see rows from both tenants"
    )


async def test_guc_does_not_leak_across_acquires(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """``set_config(..., true)`` is transaction-local. After the
    wrapper closes the transaction, the GUC must be cleared even if
    the same physical connection is handed to the next acquire.
    Regression target: a careless rewrite that drops the surrounding
    transaction would persist the GUC across acquires and quietly
    leak tenant data."""
    tenants = await _two_tenants_full()
    a = tenants["A"]
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                a["tenant_id"],
            )
            assert (
                await conn.fetchval("SELECT current_tenant_id()::text")
            ) == a["tenant_id"]
    # Subsequent acquire on the same pool: GUC must be NULL.
    async with app_pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_tenant_id()") is None
        rows = await conn.fetch(
            "SELECT * FROM approval_requests WHERE tenant_id = $1::uuid",
            a["tenant_id"],
        )
        assert rows == [], (
            "GUC leaked across acquire — would have shown rows from previous tenant"
        )


async def test_force_rls_keeps_owner_under_policy(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Without FORCE ROW LEVEL SECURITY, the table owner (which on
    a fresh testcontainer is the superuser that ran CREATE TABLE) is
    exempt from policies. PR-D adds FORCE precisely so this exemption
    is removed for non-superusers. ``rolemesh_app`` is not the owner
    in this test, but the regression we're guarding against is "we
    forgot FORCE on table X". Verify pg_class.relrowsecurity AND
    pg_class.relforcerowsecurity are both true on every tenant table."""
    expected = [
        "approval_audit_log", "approval_requests", "approval_policies",
        "safety_rules", "safety_decisions", "safety_rules_audit",
        "scheduled_tasks", "task_run_logs",
        "messages", "conversations", "sessions",
        "coworkers", "channel_bindings", "user_agent_assignments",
        "users", "oidc_user_tokens",
    ]
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY($1::text[])",
            expected,
        )
    seen = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rows}
    missing = [t for t in expected if t not in seen]
    assert not missing, f"tables missing from pg_class: {missing}"
    not_enabled = [t for t, (e, _) in seen.items() if not e]
    not_forced = [t for t, (_, f) in seen.items() if not f]
    assert not not_enabled, f"RLS not enabled on: {not_enabled}"
    assert not not_forced, f"FORCE not set on (would let owner bypass): {not_forced}"


# Silence unused-import noise on tenant_conn (kept for future tests).
assert tenant_conn is pg.tenant_conn
