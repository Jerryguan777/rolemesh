"""v6.1 §P1.2 — schema-level invariants for the new identity tables.

Asserts shape + constraints on ``user_channel_identities`` /
``link_tokens`` and the new ``scheduled_tasks.created_by_user_id``
column. The behavioural / link-flow tests live with the consumers
(``tests/auth/test_channel_identity.py``,
``tests/channels/test_telegram_link.py``) — this file only nails the
DB contract so a future ALTER cannot silently relax it.

Convention follows ``tests/test_schema_alters.py``: real Postgres
(testcontainer), no mocks, ``_get_admin_pool`` for cross-tenant
assertions.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from rolemesh.core.types import ScheduledTask
from rolemesh.db import (
    _get_admin_pool,
    create_coworker,
    create_task,
    create_tenant,
    create_user,
    get_task_by_id,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Shape — new columns / tables exist
# ---------------------------------------------------------------------------


async def _columns(table: str) -> dict[str, dict[str, object]]:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable, udt_name "
            "FROM information_schema.columns WHERE table_name = $1",
            table,
        )
    return {
        r["column_name"]: {
            "data_type": r["data_type"],
            "udt_name": r["udt_name"],
            "is_nullable": r["is_nullable"] == "YES",
        }
        for r in rows
    }


async def test_scheduled_tasks_has_created_by_user_id() -> None:
    """The new column lands as UUID NULLABLE (ON DELETE SET NULL keeps
    the row alive after the creator is gone — the cancel-before-delete
    discipline in ``delete_user`` is what makes that safe)."""
    cols = await _columns("scheduled_tasks")
    assert "created_by_user_id" in cols, (
        f"scheduled_tasks.created_by_user_id missing; columns: {sorted(cols)}"
    )
    assert cols["created_by_user_id"]["is_nullable"] is True
    assert cols["created_by_user_id"]["udt_name"] == "uuid"


async def test_user_channel_identities_table_columns() -> None:
    """The new identity table lands with the exact columns the design
    requires. Catches a future ALTER that renames or drops one of them."""
    cols = await _columns("user_channel_identities")
    expected = {"id", "tenant_id", "platform", "channel_id", "user_id", "created_at"}
    assert expected <= set(cols), (
        f"missing columns: {expected - set(cols)}; have: {sorted(cols)}"
    )
    # All non-PK identity columns are NOT NULL — the design forbids
    # half-linked rows.
    assert cols["tenant_id"]["is_nullable"] is False
    assert cols["platform"]["is_nullable"] is False
    assert cols["channel_id"]["is_nullable"] is False
    assert cols["user_id"]["is_nullable"] is False


async def test_link_tokens_table_columns() -> None:
    cols = await _columns("link_tokens")
    expected = {
        "token", "user_id", "tenant_id", "platform",
        "expires_at", "used_at", "created_at",
    }
    assert expected <= set(cols), (
        f"missing columns: {expected - set(cols)}; have: {sorted(cols)}"
    )
    # used_at is the "still-consumable" sentinel — it MUST be nullable;
    # expires_at carries the deadline, MUST NOT.
    assert cols["used_at"]["is_nullable"] is True
    assert cols["expires_at"]["is_nullable"] is False


# ---------------------------------------------------------------------------
# UNIQUE on user_channel_identities — T1.3 and its near-mutations
# ---------------------------------------------------------------------------


async def _seed_user(*, slug_tag: str) -> tuple[str, str]:
    """Return (tenant_id, user_id)."""
    t = await create_tenant(name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}")
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
    )
    return t.id, u.id


async def test_uci_unique_same_tenant_platform_channel_id_collides() -> None:
    """T1.3 — Two rows with the same (tenant_id, platform, channel_id)
    must collide at INSERT time, not silently overwrite.

    Without this constraint a race between two ``/start <token>``
    deliveries could double-bind the same Telegram account to two
    RoleMesh users.
    """
    tid, uid1 = await _seed_user(slug_tag="uci-col")
    _, uid2 = await _seed_user(slug_tag="uci-col2")
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, platform, channel_id, user_id) "
            "VALUES ($1::uuid, $2, $3, $4::uuid)",
            tid, "telegram", "111222", uid1,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO user_channel_identities "
                "(tenant_id, platform, channel_id, user_id) "
                "VALUES ($1::uuid, $2, $3, $4::uuid)",
                tid, "telegram", "111222", uid2,
            )


async def test_uci_unique_does_not_block_cross_tenant() -> None:
    """A Telegram numeric id is globally unique, but in test DBs that
    is irrelevant — what matters is the constraint scope. The UNIQUE
    is (tenant_id, platform, channel_id), so tenant A and tenant B
    can carry rows for the same channel_id without collision. Without
    this test a well-meaning hardening could swap the column order
    and accidentally over-constrain.
    """
    t1, u1 = await _seed_user(slug_tag="uci-xt1")
    t2, u2 = await _seed_user(slug_tag="uci-xt2")
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, platform, channel_id, user_id) "
            "VALUES ($1::uuid, $2, $3, $4::uuid)",
            t1, "telegram", "555", u1,
        )
        # Cross-tenant: must NOT raise.
        await conn.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, platform, channel_id, user_id) "
            "VALUES ($1::uuid, $2, $3, $4::uuid)",
            t2, "telegram", "555", u2,
        )


async def test_uci_one_user_can_bind_multiple_channel_ids() -> None:
    """Decision #13: one RoleMesh user may legitimately link more than
    one Telegram account (personal + work). The constraint must allow
    that — i.e. there is intentionally NO ``UNIQUE (user_id, platform)``.

    A near-miss mutation that adds such a constraint would surface
    here.
    """
    tid, uid = await _seed_user(slug_tag="uci-multi")
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, platform, channel_id, user_id) "
            "VALUES ($1::uuid, $2, $3, $4::uuid)",
            tid, "telegram", "personal-1", uid,
        )
        # Same user + platform, different channel id → must NOT raise.
        await conn.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, platform, channel_id, user_id) "
            "VALUES ($1::uuid, $2, $3, $4::uuid)",
            tid, "telegram", "work-2", uid,
        )


async def test_uci_user_delete_cascades_identities() -> None:
    """ON DELETE CASCADE on ``user_id``: deleting the user removes
    every identity row pointing at them. The link-token semantics rely
    on this so an unbind can be modelled as ``DELETE FROM
    user_channel_identities WHERE ...`` (decision §P1.4) without a
    separate audit trail to keep in sync.
    """
    tid, uid = await _seed_user(slug_tag="uci-del")
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_channel_identities "
            "(tenant_id, platform, channel_id, user_id) "
            "VALUES ($1::uuid, $2, $3, $4::uuid)",
            tid, "telegram", "999", uid,
        )
        await conn.execute("DELETE FROM users WHERE id = $1::uuid", uid)
        rows = await conn.fetch(
            "SELECT 1 FROM user_channel_identities WHERE user_id = $1::uuid",
            uid,
        )
        assert rows == []


# ---------------------------------------------------------------------------
# ScheduledTask.status Literal extension (T1.14)
# ---------------------------------------------------------------------------


async def test_scheduled_task_status_cancelled_round_trips() -> None:
    """T1.14 — A task created with ``status='cancelled'`` survives a
    DB round-trip with the value intact. The schema column is plain
    TEXT (no CHECK constraint), so the assertion is on the type-side
    Literal + the row mapper: both must accept and preserve the value.
    """
    t = await create_tenant(name="T", slug=f"st-cncl-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        tenant_id=t.id,
        coworker_id=cw.id,
        prompt="ping",
        schedule_type="interval",
        schedule_value="60",
        context_mode="isolated",
        status="cancelled",
    )
    await create_task(task)
    fetched = await get_task_by_id(task.id, tenant_id=t.id)
    assert fetched is not None
    assert fetched.status == "cancelled"
