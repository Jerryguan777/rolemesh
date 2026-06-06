"""DB-layer visibility: backfill ordering + the list predicate's
three-valued NULL logic.

feat/roles PR3. The trap the plan flagged is the backfill: existing
rows must end up ``'shared'`` (so they stay visible to members after the
upgrade) while NEW rows default to ``'private'``. We assert both halves
against the real schema, and we assert the list predicate does not leak a
NULL-attributed private row to a member (NULL never equals the user).
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.db import (
    create_coworker,
    create_skill,
    create_tenant,
    create_user,
    get_coworkers_for_tenant,
    list_skills_for_tenant,
)
from rolemesh.db._pool import admin_conn

pytestmark = pytest.mark.usefixtures("test_db")


async def _tenant() -> str:
    t = await create_tenant(name="T", slug=f"bf-{uuid.uuid4().hex[:8]}")
    return t.id


# ---------------------------------------------------------------------------
# Column + constraint exist; new rows default private.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_coworker_row_defaults_private_at_db_level() -> None:
    """An INSERT that omits ``visibility`` lands 'private' — proves the
    SET DEFAULT 'private' flip won (not the transient ADD ... 'shared').

    Inserts via raw SQL (bypassing ``create_coworker``'s own default) so
    the assertion is about the COLUMN default, not the Python default."""
    tid = await _tenant()
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "INSERT INTO coworkers (tenant_id, name, folder) "
            "VALUES ($1::uuid, $2, $3) RETURNING visibility",
            tid, "raw", f"raw-{uuid.uuid4().hex[:8]}",
        )
    assert row is not None
    assert row["visibility"] == "private"


@pytest.mark.asyncio
async def test_new_skill_row_defaults_private_at_db_level() -> None:
    tid = await _tenant()
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "INSERT INTO skills (tenant_id, name) "
            "VALUES ($1::uuid, $2) RETURNING visibility",
            tid, f"sk{uuid.uuid4().hex[:6]}",
        )
    assert row is not None
    assert row["visibility"] == "private"


@pytest.mark.asyncio
async def test_visibility_check_constraint_rejects_garbage() -> None:
    """The CHECK constraint must reject any value outside the domain."""
    tid = await _tenant()
    import asyncpg

    async with admin_conn() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO coworkers (tenant_id, name, folder, visibility) "
                "VALUES ($1::uuid, $2, $3, 'public')",
                tid, "bad", f"bad-{uuid.uuid4().hex[:8]}",
            )


# ---------------------------------------------------------------------------
# Backfill: a row that existed BEFORE the column was added ends 'shared'.
# We simulate the pre-migration state by stripping the column, inserting a
# legacy row, then re-running the migration DDL and asserting it backfilled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_makes_preexisting_coworker_shared() -> None:
    """Drop the column (simulate a pre-PR3 row), insert a legacy
    coworker, then replay the exact two-step migration DDL. The legacy
    row must come out 'shared' — NOT 'private' (which would hide it from
    every member). This is the regression the plan called the trap."""
    tid = await _tenant()
    folder = f"legacy-{uuid.uuid4().hex[:8]}"
    async with admin_conn() as conn:
        # Simulate the pre-migration table shape.
        await conn.execute(
            "ALTER TABLE coworkers DROP COLUMN visibility"
        )
        await conn.execute(
            "INSERT INTO coworkers (tenant_id, name, folder) "
            "VALUES ($1::uuid, $2, $3)",
            tid, "legacy", folder,
        )
        # Replay the migration exactly as schema.py orders it.
        await conn.execute(
            "ALTER TABLE coworkers "
            "ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'shared'"
        )
        await conn.execute(
            "ALTER TABLE coworkers ALTER COLUMN visibility SET DEFAULT 'private'"
        )
        legacy_vis = await conn.fetchval(
            "SELECT visibility FROM coworkers WHERE folder = $1", folder
        )
        # A row inserted AFTER the flip must be private.
        await conn.execute(
            "INSERT INTO coworkers (tenant_id, name, folder) "
            "VALUES ($1::uuid, $2, $3)",
            tid, "fresh", f"fresh-{uuid.uuid4().hex[:8]}",
        )
        fresh_vis = await conn.fetchval(
            "SELECT visibility FROM coworkers WHERE name = 'fresh' "
            "AND tenant_id = $1::uuid",
            tid,
        )
    assert legacy_vis == "shared", "pre-existing row must backfill to shared"
    assert fresh_vis == "private", "post-flip row must default private"


# ---------------------------------------------------------------------------
# List predicate NULL three-valued logic (data layer, no HTTP).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_predicate_hides_null_attributed_private_from_member() -> None:
    tid = await _tenant()
    me = await create_user(
        tenant_id=tid, name="U", email=f"u-{uuid.uuid4().hex[:6]}@x.com",
        role="member",
    )
    mine = await create_coworker(
        tenant_id=tid, name="mine", folder=f"m-{uuid.uuid4().hex[:8]}",
        created_by_user_id=me.id, visibility="private",
    )
    orphan = await create_coworker(
        tenant_id=tid, name="orphan", folder=f"o-{uuid.uuid4().hex[:8]}",
        created_by_user_id=None, visibility="private",
    )
    shared = await create_coworker(
        tenant_id=tid, name="shared", folder=f"s-{uuid.uuid4().hex[:8]}",
        created_by_user_id=None, visibility="shared",
    )

    filtered = await get_coworkers_for_tenant(
        tid, requesting_user_id=me.id, include_all=False,
    )
    ids = {c.id for c in filtered}
    assert mine.id in ids
    assert shared.id in ids
    # The NULL created_by + private row must NOT leak: ``created_by = $2``
    # is NULL (not TRUE) for it, so the predicate excludes it.
    assert orphan.id not in ids

    # include_all (manager path) returns everything, orphan included.
    all_rows = await get_coworkers_for_tenant(tid, include_all=True)
    assert orphan.id in {c.id for c in all_rows}


@pytest.mark.asyncio
async def test_skill_list_predicate_hides_null_attributed_private() -> None:
    from rolemesh.core.types import SkillFile as SkillFileDataclass

    tid = await _tenant()
    me = await create_user(
        tenant_id=tid, name="U", email=f"u-{uuid.uuid4().hex[:6]}@x.com",
        role="member",
    )
    files = {"SKILL.md": SkillFileDataclass(path="SKILL.md", content="b")}
    orphan = await create_skill(
        tenant_id=tid, name=f"orph{uuid.uuid4().hex[:6]}",
        frontmatter_common={"description": "x" * 40}, frontmatter_backend={},
        files=files, created_by_user_id=None, visibility="private",
    )
    mine = await create_skill(
        tenant_id=tid, name=f"mine{uuid.uuid4().hex[:6]}",
        frontmatter_common={"description": "x" * 40}, frontmatter_backend={},
        files=files, created_by_user_id=me.id, visibility="private",
    )

    filtered = await list_skills_for_tenant(
        tid, requesting_user_id=me.id, include_all=False,
    )
    ids = {s.id for s in filtered}
    assert mine.id in ids
    assert orphan.id not in ids


# ---------------------------------------------------------------------------
# Validation: the DB helpers reject an out-of-domain visibility value
# BEFORE the round-trip to Postgres.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_coworker_rejects_bad_visibility() -> None:
    tid = await _tenant()
    with pytest.raises(ValueError, match="visibility"):
        await create_coworker(
            tenant_id=tid, name="x", folder=f"x-{uuid.uuid4().hex[:8]}",
            visibility="everyone",
        )
