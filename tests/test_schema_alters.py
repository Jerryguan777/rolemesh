"""v1.1 §2.2 — existing-table ALTERs.

What is asserted here:

* The four new columns / constraint land on the right tables:
  - ``coworkers.model_id`` (FK -> ``models.id``, NULLABLE)
  - ``coworkers.created_by_user_id`` (FK -> ``users.id``, NULLABLE)
  - ``skills.created_by_user_id`` (FK -> ``users.id``, NULLABLE)
    — renamed in-place from the legacy ``created_by`` column on
    pre-existing dev DBs; on a fresh testcontainer ``CREATE TABLE``
    already uses the new name.
  - ``messages.run_id`` (FK -> ``runs.id``, NULLABLE)
  - ``skills_tenant_name_unique`` UNIQUE (tenant_id, name)

* The ALTER pass is *idempotent* — running ``_create_schema(conn)`` a
  second time on the same connection does not raise. This is the
  greenfield contract: schema.py is the source of truth and re-runs
  safely on dev DBs that were created from an earlier version.

* The new UNIQUE constraint is enforced (two skills with the same
  tenant_id + name -> IntegrityError).

* The new FK is enforced (``messages.run_id`` pointing at a
  non-existent run -> IntegrityError).

These tests use the testcontainer ``test_db`` fixture (clean schema
per test) rather than the dev DB; per CLAUDE.md "测试理念", no mock
postgres is allowed for schema-level invariants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from rolemesh.db import (
    _get_admin_pool,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)
from rolemesh.db.schema import _create_schema

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Idempotency — schema.py must survive a second invocation
# ---------------------------------------------------------------------------


async def test_create_schema_is_idempotent() -> None:
    """Running ``_create_schema(conn)`` a second time on a DB that
    already has every v1.1 object must not raise.

    The classic landmines are:
    - ``CREATE POLICY`` (PG has no IF NOT EXISTS form; the helper
      uses ``DROP POLICY IF EXISTS … ; CREATE POLICY …``).
    - ``ALTER TABLE ADD CONSTRAINT`` (the skills tenant-name UNIQUE
      constraint; same story — wrapped in ``DO $$ … IF NOT EXISTS``).
    - ``ALTER TABLE RENAME COLUMN`` for the skills.created_by →
      created_by_user_id rename, guarded behind ``information_schema``
      lookups.
    """
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        # First call is already done by the fixture; we hammer a
        # second one to assert no exception.
        await _create_schema(conn)


# ---------------------------------------------------------------------------
# Columns exist with the right type / nullability
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


async def test_coworkers_has_model_id_and_created_by_user_id() -> None:
    cols = await _columns("coworkers")
    assert "model_id" in cols, f"coworkers.model_id missing; columns: {sorted(cols)}"
    assert "created_by_user_id" in cols, (
        f"coworkers.created_by_user_id missing; columns: {sorted(cols)}"
    )
    # L6 strong: audit / FK columns must be NULLABLE (bootstrap can't
    # produce a real user id in every code path).
    assert cols["model_id"]["is_nullable"] is True
    assert cols["created_by_user_id"]["is_nullable"] is True
    # Both are UUID — udt_name uses PG's internal name.
    assert cols["model_id"]["udt_name"] == "uuid"
    assert cols["created_by_user_id"]["udt_name"] == "uuid"


async def test_skills_has_created_by_user_id_and_no_legacy_created_by() -> None:
    """The rename moved the column wholesale — the old name is gone."""
    cols = await _columns("skills")
    assert "created_by_user_id" in cols, (
        f"skills.created_by_user_id missing; columns: {sorted(cols)}"
    )
    assert "created_by" not in cols, (
        "legacy skills.created_by still exists — rename DO-block "
        "did not run on this DB"
    )
    assert cols["created_by_user_id"]["is_nullable"] is True
    assert cols["created_by_user_id"]["udt_name"] == "uuid"


async def test_messages_has_run_id() -> None:
    cols = await _columns("messages")
    assert "run_id" in cols, f"messages.run_id missing; columns: {sorted(cols)}"
    assert cols["run_id"]["is_nullable"] is True
    assert cols["run_id"]["udt_name"] == "uuid"


# ---------------------------------------------------------------------------
# Default values — new columns must come up NULL on insert
# ---------------------------------------------------------------------------


async def test_new_columns_default_to_null() -> None:
    """``ADD COLUMN`` without a DEFAULT must leave existing + new rows
    with NULL. The smoke is: insert a coworker via the existing path,
    verify the new columns came up NULL.

    This is the "we did not accidentally backfill" assertion. The
    greenfield rule is no backfill — Phase 1+ writers will populate
    the columns explicitly.
    """
    t = await create_tenant(name="A", slug=f"alters-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT model_id, created_by_user_id FROM coworkers "
            "WHERE id = $1::uuid",
            cw.id,
        )
    assert row is not None
    assert row["model_id"] is None
    assert row["created_by_user_id"] is None


# ---------------------------------------------------------------------------
# skills_tenant_name_unique — the constraint actually fires
# ---------------------------------------------------------------------------


async def test_skills_tenant_name_unique_constraint_fires() -> None:
    """Two skills under the same tenant_id with the same name on
    *different* coworkers must collide at INSERT time. Without the
    new tenant-level UNIQUE, this would be allowed (the legacy
    UNIQUE was (coworker_id, name) only) — a future 03b "skills go
    per-tenant" migration would then be blocked by duplicate names
    that should never have been written.
    """
    t = await create_tenant(name="T", slug=f"uniq-{uuid.uuid4().hex[:6]}")
    cw1 = await create_coworker(
        tenant_id=t.id, name="CW1", folder=f"cw1-{uuid.uuid4().hex[:6]}"
    )
    cw2 = await create_coworker(
        tenant_id=t.id, name="CW2", folder=f"cw2-{uuid.uuid4().hex[:6]}"
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO skills (tenant_id, coworker_id, name) "
            "VALUES ($1::uuid, $2::uuid, $3)",
            t.id, cw1.id, "shared-name",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO skills (tenant_id, coworker_id, name) "
                "VALUES ($1::uuid, $2::uuid, $3)",
                t.id, cw2.id, "shared-name",
            )


async def test_skills_tenant_name_unique_does_not_block_cross_tenant() -> None:
    """A different tenant CAN reuse the same skill name — the UNIQUE
    is on (tenant_id, name), not on name alone. Without this test a
    well-meaning hardening pass could over-constrain and break the
    tenant-isolation promise from the user's perspective ('I named my
    skill X and someone else's tenant blocked me')."""
    t1 = await create_tenant(name="T1", slug=f"x1-{uuid.uuid4().hex[:6]}")
    t2 = await create_tenant(name="T2", slug=f"x2-{uuid.uuid4().hex[:6]}")
    cw1 = await create_coworker(
        tenant_id=t1.id, name="CW", folder=f"cw1-{uuid.uuid4().hex[:6]}"
    )
    cw2 = await create_coworker(
        tenant_id=t2.id, name="CW", folder=f"cw2-{uuid.uuid4().hex[:6]}"
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO skills (tenant_id, coworker_id, name) "
            "VALUES ($1::uuid, $2::uuid, $3)",
            t1.id, cw1.id, "duplicate",
        )
        # Cross-tenant: should not raise.
        await conn.execute(
            "INSERT INTO skills (tenant_id, coworker_id, name) "
            "VALUES ($1::uuid, $2::uuid, $3)",
            t2.id, cw2.id, "duplicate",
        )


# ---------------------------------------------------------------------------
# messages.run_id FK — must reject a dangling reference
# ---------------------------------------------------------------------------


async def _seed_for_message(*, slug_tag: str) -> dict[str, str]:
    t = await create_tenant(name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}")
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    binding = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id,
        channel_type="telegram", credentials={"bot_token": "x"},
    )
    conv = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id, channel_binding_id=binding.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    return {
        "tenant_id": t.id, "user_id": u.id, "coworker_id": cw.id,
        "conversation_id": conv.id,
    }


async def test_messages_run_id_fk_rejects_dangling_uuid() -> None:
    """Pointing ``messages.run_id`` at a uuid that does not exist in
    the ``runs`` table must fail at INSERT time. This is the
    second-layer guard: 01a will write to ``runs`` first then
    ``messages.run_id`` — if the order ever flips, this test
    surfaces the violation immediately."""
    s = await _seed_for_message(slug_tag="msg-fk")
    bogus_run_id = str(uuid.uuid4())  # no row exists
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO messages "
                "(id, tenant_id, conversation_id, timestamp, run_id) "
                "VALUES ($1, $2::uuid, $3::uuid, $4, $5::uuid)",
                "msg-1", s["tenant_id"], s["conversation_id"],
                datetime.now(UTC), bogus_run_id,
            )


async def test_messages_run_id_accepts_real_run_and_null() -> None:
    """Two valid shapes: a real ``runs`` row, and NULL (the default
    on existing rows). Both must round-trip cleanly."""
    s = await _seed_for_message(slug_tag="msg-ok")
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        run_id = await conn.fetchval(
            "INSERT INTO runs (tenant_id, conversation_id, status) "
            "VALUES ($1::uuid, $2::uuid, $3) RETURNING id",
            s["tenant_id"], s["conversation_id"], "running",
        )
        # Linked message.
        await conn.execute(
            "INSERT INTO messages "
            "(id, tenant_id, conversation_id, timestamp, run_id) "
            "VALUES ($1, $2::uuid, $3::uuid, $4, $5::uuid)",
            "msg-linked", s["tenant_id"], s["conversation_id"],
            datetime.now(UTC), run_id,
        )
        # NULL run_id message — the column must keep accepting NULL
        # for legacy / external-channel paths.
        await conn.execute(
            "INSERT INTO messages "
            "(id, tenant_id, conversation_id, timestamp) "
            "VALUES ($1, $2::uuid, $3::uuid, $4)",
            "msg-null", s["tenant_id"], s["conversation_id"],
            datetime.now(UTC),
        )
        rows = await conn.fetch(
            "SELECT id, run_id FROM messages "
            "WHERE tenant_id = $1::uuid ORDER BY id",
            s["tenant_id"],
        )
    by_id = {r["id"]: r["run_id"] for r in rows}
    assert str(by_id["msg-linked"]) == str(run_id)
    assert by_id["msg-null"] is None


# ---------------------------------------------------------------------------
# Models FK on coworkers.model_id is enforced
# ---------------------------------------------------------------------------


async def test_coworkers_model_id_fk_rejects_dangling_uuid() -> None:
    """Setting ``coworkers.model_id`` to a uuid that doesn't exist in
    ``models`` must fail."""
    t = await create_tenant(name="T", slug=f"model-fk-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    bogus = str(uuid.uuid4())
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "UPDATE coworkers SET model_id = $1::uuid WHERE id = $2::uuid",
                bogus, cw.id,
            )


async def test_coworkers_model_id_fk_accepts_seeded_model() -> None:
    """A real seeded ``models`` row must be assignable to ``model_id``.
    Acts as a smoke for the seed running at all — without it the
    coworker create wizard in Phase 2 would have nothing to select."""
    t = await create_tenant(name="T", slug=f"model-ok-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:6]}"
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        model_id = await conn.fetchval(
            "SELECT id FROM models WHERE provider = $1 AND model_id = $2",
            "anthropic", "claude-opus-4-7",
        )
        assert model_id is not None, "platform models seed missing claude-opus-4-7"
        await conn.execute(
            "UPDATE coworkers SET model_id = $1::uuid WHERE id = $2::uuid",
            model_id, cw.id,
        )
        result = await conn.fetchval(
            "SELECT model_id FROM coworkers WHERE id = $1::uuid", cw.id
        )
        assert str(result) == str(model_id)
