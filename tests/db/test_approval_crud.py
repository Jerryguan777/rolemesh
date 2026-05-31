"""DB-layer tests for HITL approval: CRUD, decision-race idempotency, and
cross-tenant isolation at both the application (WHERE) and RLS (policy)
layers.

The RLS-layer tests use a dedicated ``rolemesh_app`` pool (NOBYPASSRLS) —
the default test pool connects as the testcontainer superuser, which
bypasses RLS and so can only exercise the explicit ``WHERE tenant_id``
belt, not the policy braces. Both layers matter: the WHERE clause guards
the happy path, the policy guards a query that forgets it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest

from rolemesh.db import (
    _get_pool,
    admin_conn,
    create_approval_policy,
    create_approval_request,
    create_coworker,
    create_tenant,
    create_user,
    delete_approval_policy,
    get_approval_policy,
    get_approval_request,
    list_approval_policies,
    list_pending_requests_all_tenants,
    list_pending_requests_for_tenant,
    resolve_approval_request,
    update_approval_policy,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

pytestmark = pytest.mark.usefixtures("test_db")


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


async def _tenant_with_coworker(tag: str) -> dict[str, str]:
    t = await create_tenant(name=f"T{tag}", slug=f"apr-{tag.lower()}-{uuid.uuid4().hex[:6]}")
    u = await create_user(
        tenant_id=t.id, name=f"U{tag}",
        email=f"u-{tag.lower()}-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name=f"CW{tag}", folder=f"cw-{tag.lower()}-{uuid.uuid4().hex[:6]}",
    )
    return {"tenant_id": t.id, "user_id": u.id, "coworker_id": cw.id}


@pytest.fixture
async def app_pool(pg_url: str) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record], None]:
    """A pool that logs in as ``rolemesh_app`` (NOBYPASSRLS) so policies bind."""
    superuser_pool = _get_pool()
    async with superuser_pool.acquire() as conn:
        await conn.execute("ALTER USER rolemesh_app PASSWORD 'test'")
    rewritten = pg_url.replace("test:test@", "rolemesh_app:test@", 1)
    pool = await asyncpg.create_pool(rewritten, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Policy CRUD round-trips
# ---------------------------------------------------------------------------


async def test_create_policy_defaults_to_always_true() -> None:
    t = await _tenant_with_coworker("A")
    p = await create_approval_policy(
        tenant_id=t["tenant_id"], mcp_server_name="stripe", tool_name="charge",
    )
    assert p.condition_expr == {"always": True}
    assert p.enabled is True
    assert p.priority == 0
    # Round-trips through jsonb as a real dict, not a string.
    fetched = await get_approval_policy(p.id, tenant_id=t["tenant_id"])
    assert fetched is not None
    assert fetched.condition_expr == {"always": True}


async def test_create_policy_preserves_nested_condition_jsonb() -> None:
    # A non-trivial condition must survive the json.dumps -> jsonb -> parse
    # round-trip byte-for-byte (structure, types, nesting).
    t = await _tenant_with_coworker("A")
    expr = {"and": [
        {"field": "amount", "op": ">", "value": 100},
        {"or": [{"field": "currency", "op": "in", "value": ["USD", "EUR"]}]},
    ]}
    p = await create_approval_policy(
        tenant_id=t["tenant_id"], mcp_server_name="stripe", tool_name="*",
        condition_expr=expr, priority=7,
    )
    fetched = await get_approval_policy(p.id, tenant_id=t["tenant_id"])
    assert fetched is not None
    assert fetched.condition_expr == expr
    assert fetched.priority == 7


async def test_update_policy_changes_fields_and_bumps_updated_at() -> None:
    t = await _tenant_with_coworker("A")
    p = await create_approval_policy(
        tenant_id=t["tenant_id"], mcp_server_name="stripe", tool_name="charge",
    )
    updated = await update_approval_policy(
        p.id, tenant_id=t["tenant_id"], enabled=False, priority=99,
        condition_expr={"field": "x", "op": "==", "value": 1},
    )
    assert updated is not None
    assert updated.enabled is False
    assert updated.priority == 99
    assert updated.condition_expr == {"field": "x", "op": "==", "value": 1}
    assert updated.updated_at >= p.updated_at


async def test_list_enabled_only_filters_disabled() -> None:
    t = await _tenant_with_coworker("A")
    await create_approval_policy(
        tenant_id=t["tenant_id"], mcp_server_name="s", tool_name="a", enabled=True,
    )
    await create_approval_policy(
        tenant_id=t["tenant_id"], mcp_server_name="s", tool_name="b", enabled=False,
    )
    all_p = await list_approval_policies(t["tenant_id"])
    enabled = await list_approval_policies(t["tenant_id"], enabled_only=True)
    assert len(all_p) == 2
    assert [p.tool_name for p in enabled] == ["a"]


async def test_delete_policy_returns_true_then_false() -> None:
    t = await _tenant_with_coworker("A")
    p = await create_approval_policy(
        tenant_id=t["tenant_id"], mcp_server_name="s", tool_name="a",
    )
    assert await delete_approval_policy(p.id, tenant_id=t["tenant_id"]) is True
    # Second delete affects zero rows — must not falsely report success.
    assert await delete_approval_policy(p.id, tenant_id=t["tenant_id"]) is False
    assert await get_approval_policy(p.id, tenant_id=t["tenant_id"]) is None


# ---------------------------------------------------------------------------
# Application-layer tenant scoping (the WHERE belt — testable under superuser)
# ---------------------------------------------------------------------------


async def test_get_policy_scoped_to_tenant() -> None:
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    p = await create_approval_policy(
        tenant_id=a["tenant_id"], mcp_server_name="s", tool_name="a",
    )
    # B asks for A's policy id -> the WHERE tenant_id clause returns None
    # from the DB itself, no post-filter.
    assert await get_approval_policy(p.id, tenant_id=b["tenant_id"]) is None
    assert await get_approval_policy(p.id, tenant_id=a["tenant_id"]) is not None


async def test_list_policies_does_not_leak_across_tenant() -> None:
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    await create_approval_policy(tenant_id=a["tenant_id"], mcp_server_name="s", tool_name="a")
    await create_approval_policy(tenant_id=b["tenant_id"], mcp_server_name="s", tool_name="b")
    a_list = await list_approval_policies(a["tenant_id"])
    assert {p.tool_name for p in a_list} == {"a"}


# ---------------------------------------------------------------------------
# Request CRUD + decision-race idempotency
# ---------------------------------------------------------------------------


async def test_create_request_round_trips_action_and_null_approver() -> None:
    t = await _tenant_with_coworker("A")
    action = {"tool_name": "charge", "params": {"amount": 500, "currency": "USD"}}
    req = await create_approval_request(
        tenant_id=t["tenant_id"], coworker_id=t["coworker_id"], job_id="job-1",
        mcp_server_name="stripe", action=action, expires_at=_future(),
        user_id=None, action_summary="charge $500",
    )
    assert req.status == "pending"
    assert req.user_id is None          # null approver persisted, not coerced
    fetched = await get_approval_request(req.id, tenant_id=t["tenant_id"])
    assert fetched is not None
    assert fetched.action == action
    assert fetched.action_summary == "charge $500"


async def test_resolve_is_first_wins_idempotent() -> None:
    # The §8 decision race: a late approve click vs a timeout-expiry. Only the
    # first transition out of 'pending' may take effect; the second sees no
    # pending row and returns None. Both sides converge on the first writer.
    t = await _tenant_with_coworker("A")
    req = await create_approval_request(
        tenant_id=t["tenant_id"], coworker_id=t["coworker_id"], job_id="job-1",
        mcp_server_name="stripe", action={"tool_name": "charge", "params": {}},
        expires_at=_future(), user_id=t["user_id"],
    )
    first = await resolve_approval_request(
        req.id, tenant_id=t["tenant_id"], status="approved",
        decided_by=t["user_id"], note="ok",
    )
    assert first is not None and first.status == "approved"
    assert first.decided_by == t["user_id"]
    assert first.decided_at is not None

    # Racing expiry tries to flip the same row -> zero rows, None.
    second = await resolve_approval_request(
        req.id, tenant_id=t["tenant_id"], status="expired",
    )
    assert second is None

    # DB still reflects only the first decision.
    final = await get_approval_request(req.id, tenant_id=t["tenant_id"])
    assert final is not None and final.status == "approved" and final.note == "ok"


async def test_resolve_rejects_non_terminal_status() -> None:
    t = await _tenant_with_coworker("A")
    req = await create_approval_request(
        tenant_id=t["tenant_id"], coworker_id=t["coworker_id"], job_id="j",
        mcp_server_name="s", action={"tool_name": "t", "params": {}},
        expires_at=_future(),
    )
    with pytest.raises(ValueError, match="terminal"):
        await resolve_approval_request(req.id, tenant_id=t["tenant_id"], status="pending")


async def test_resolve_scoped_to_tenant() -> None:
    # IDOR belt: tenant B cannot resolve tenant A's request even with the id.
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    req = await create_approval_request(
        tenant_id=a["tenant_id"], coworker_id=a["coworker_id"], job_id="j",
        mcp_server_name="s", action={"tool_name": "t", "params": {}},
        expires_at=_future(),
    )
    assert await resolve_approval_request(
        req.id, tenant_id=b["tenant_id"], status="approved",
    ) is None
    # A's request remains pending.
    still = await get_approval_request(req.id, tenant_id=a["tenant_id"])
    assert still is not None and still.status == "pending"


async def test_pending_lists_scope_correctly() -> None:
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    for tag in (a, b):
        await create_approval_request(
            tenant_id=tag["tenant_id"], coworker_id=tag["coworker_id"], job_id="j",
            mcp_server_name="s", action={"tool_name": "t", "params": {}},
            expires_at=_future(),
        )
    # Resolve A's so only B's stays pending for A's tenant view.
    a_pending = await list_pending_requests_for_tenant(a["tenant_id"])
    assert len(a_pending) == 1
    await resolve_approval_request(
        a_pending[0].id, tenant_id=a["tenant_id"], status="cancelled",
    )
    assert await list_pending_requests_for_tenant(a["tenant_id"]) == []
    assert len(await list_pending_requests_for_tenant(b["tenant_id"])) == 1

    # admin_conn cross-tenant scan (restart recovery) sees B's pending row,
    # across tenants, regardless of any GUC.
    all_pending = await list_pending_requests_all_tenants()
    assert len(all_pending) == 1
    assert all_pending[0].tenant_id == b["tenant_id"]


# ---------------------------------------------------------------------------
# RLS layer — the policy braces (tested under rolemesh_app, NOBYPASSRLS)
# ---------------------------------------------------------------------------


async def test_rls_blocks_cross_tenant_policy_select(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    await create_approval_policy(tenant_id=b["tenant_id"], mcp_server_name="s", tool_name="a")
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", a["tenant_id"],
        )
        rows = await conn.fetch(
            "SELECT * FROM approval_policies WHERE tenant_id = $1::uuid",
            b["tenant_id"],
        )
    assert rows == [], "RLS failed: tenant A saw tenant B's approval_policies"


async def test_rls_blocks_cross_tenant_request_insert(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", a["tenant_id"],
        )
        with pytest.raises(
            (asyncpg.InsufficientPrivilegeError, asyncpg.CheckViolationError)
        ):
            await conn.execute(
                "INSERT INTO approval_requests "
                "(tenant_id, coworker_id, job_id, mcp_server_name, action, expires_at) "
                "VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb, now())",
                b["tenant_id"],          # wrong tenant while bound to A
                b["coworker_id"],
                "job-x", "stripe", "{}",
            )


async def test_rls_unset_guc_hides_all_requests(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    # Fail-closed: a connection that forgets to set the tenant GUC must see
    # zero approval rows, not all of them.
    t = await _tenant_with_coworker("A")
    await create_approval_request(
        tenant_id=t["tenant_id"], coworker_id=t["coworker_id"], job_id="j",
        mcp_server_name="s", action={"tool_name": "t", "params": {}},
        expires_at=_future(),
    )
    async with app_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM approval_requests")
    assert rows == [], "RLS fail-closed broken: unset GUC exposed approval_requests"


async def test_rls_force_flags_set_on_both_tables() -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY($1::text[])",
            ["approval_policies", "approval_requests"],
        )
    seen = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rows}
    assert set(seen) == {"approval_policies", "approval_requests"}
    for table, (enabled, forced) in seen.items():
        assert enabled, f"RLS not enabled on {table}"
        assert forced, f"FORCE not set on {table} (owner could bypass)"


# ---------------------------------------------------------------------------
# admin_conn sees across tenants (the recovery/expiry carve-out)
# ---------------------------------------------------------------------------


async def test_admin_conn_sees_both_tenants_pending() -> None:
    a = await _tenant_with_coworker("A")
    b = await _tenant_with_coworker("B")
    for tag in (a, b):
        await create_approval_request(
            tenant_id=tag["tenant_id"], coworker_id=tag["coworker_id"], job_id="j",
            mcp_server_name="s", action={"tool_name": "t", "params": {}},
            expires_at=_future(),
        )
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT DISTINCT tenant_id FROM approval_requests")
    assert len({str(r["tenant_id"]) for r in rows}) == 2
