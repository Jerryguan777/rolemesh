"""Skills DB layer: schema constraints, RLS isolation (incl. transitive
on skill_files), SECURITY DEFINER cross-tenant trigger, CRUD happy paths,
and the SKILL.md application invariant.

Runs against the testcontainer Postgres. Two pools at play:

* The default test pool connects as the testcontainer's superuser and
  bypasses RLS — used to seed cross-tenant fixtures so we can poke
  at boundaries.
* ``app_pool`` connects as ``rolemesh_app`` (NOBYPASSRLS) and is the
  only place where RLS actually fires.

Tests are deliberately adversarial: each one tries to do something
the design says must fail, and asserts it does.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import asyncpg
import pytest

from rolemesh.core.types import SkillFile
from rolemesh.db.pg import (
    _get_pool,
    create_coworker,
    create_skill,
    create_tenant,
    delete_skill,
    delete_skill_file,
    get_skill,
    list_skills_for_coworker,
    set_skill_file,
    update_skill,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Fixtures: tenant chain + RLS-bound pool
# ---------------------------------------------------------------------------


_GOOD_DESC = "When the user asks for a code review of the staged diff."


def _basic_files() -> dict[str, SkillFile]:
    """A minimal valid skill file map: just SKILL.md."""
    return {
        "SKILL.md": SkillFile(
            path="SKILL.md",
            content="# Workflow\nDo a thing.\n",
            mime_type="text/markdown",
        ),
    }


async def _make_tenant_with_coworker(tag: str) -> tuple[str, str]:
    t = await create_tenant(name=f"T{tag}", slug=f"sk-{tag}-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id,
        name=f"CW{tag}",
        folder=f"cw-{tag}-{uuid.uuid4().hex[:6]}",
    )
    return t.id, cw.id


@pytest.fixture
async def app_pool(pg_url: str) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record], None]:
    """rolemesh_app pool — NOBYPASSRLS — used for adversarial RLS tests."""
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
# CRUD happy path
# ---------------------------------------------------------------------------


async def test_create_get_skill_round_trip() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("crt")
    files = {
        "SKILL.md": SkillFile(path="SKILL.md", content="body"),
        "reference.md": SkillFile(path="reference.md", content="ref"),
    }
    created = await create_skill(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        name="code-review",
        frontmatter_common={"name": "code-review", "description": _GOOD_DESC},
        frontmatter_backend={"claude": {"argument-hint": "[file]"}},
        files=files,
    )
    assert created.name == "code-review"
    assert set(created.files) == {"SKILL.md", "reference.md"}
    assert created.frontmatter_backend == {"claude": {"argument-hint": "[file]"}}

    fetched = await get_skill(created.id, tenant_id=tenant_id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.files["SKILL.md"].content == "body"


async def test_list_filters_disabled_when_requested() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("flt")
    enabled = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="enabled-skill",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(), enabled=True,
    )
    disabled = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="disabled-skill",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(), enabled=False,
    )
    all_skills = await list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, enabled_only=False
    )
    assert {s.id for s in all_skills} == {enabled.id, disabled.id}

    only_enabled = await list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, enabled_only=True
    )
    assert {s.id for s in only_enabled} == {enabled.id}


async def test_update_replaces_files_atomically() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("upd")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="v1"),
            "old.md": SkillFile(path="old.md", content="dropped"),
        },
    )
    updated = await update_skill(
        s.id,
        tenant_id=tenant_id,
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="v2"),
            "new.md": SkillFile(path="new.md", content="kept"),
        },
    )
    assert updated is not None
    assert set(updated.files) == {"SKILL.md", "new.md"}
    assert updated.files["SKILL.md"].content == "v2"


async def test_set_skill_file_upserts() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("upsert")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    new_file = await set_skill_file(
        s.id, "extra.md", tenant_id=tenant_id, content="hello"
    )
    assert new_file is not None
    assert new_file.path == "extra.md"

    overwritten = await set_skill_file(
        s.id, "extra.md", tenant_id=tenant_id, content="hello v2"
    )
    assert overwritten is not None
    assert overwritten.content == "hello v2"


async def test_delete_skill_file_refuses_skill_md() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("rfs")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    with pytest.raises(ValueError, match="SKILL.md cannot be deleted"):
        await delete_skill_file(s.id, "SKILL.md", tenant_id=tenant_id)


async def test_delete_skill_file_returns_false_for_unknown() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("rfn")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    assert await delete_skill_file(s.id, "nope.md", tenant_id=tenant_id) is False


async def test_delete_skill_cascades_to_files() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("dcs")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="b"),
            "ref.md": SkillFile(path="ref.md", content="r"),
        },
    )
    assert await delete_skill(s.id, tenant_id=tenant_id) is True
    # File rows should be gone — query via admin to bypass RLS just in case.
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid", s.id
        )
    assert rows == []


async def test_delete_coworker_cascades_to_skills_and_files() -> None:
    """coworkers ON DELETE CASCADE must reach the file table too."""
    tenant_id, coworker_id = await _make_tenant_with_coworker("cwd")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM coworkers WHERE id = $1::uuid", coworker_id
        )
        skills_left = await conn.fetch(
            "SELECT * FROM skills WHERE id = $1::uuid", s.id
        )
        files_left = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid", s.id
        )
    assert skills_left == []
    assert files_left == []


# ---------------------------------------------------------------------------
# SKILL.md application invariant
# ---------------------------------------------------------------------------


async def test_create_without_skill_md_is_rejected() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("inv")
    with pytest.raises(ValueError, match="SKILL.md"):
        await create_skill(
            tenant_id=tenant_id, coworker_id=coworker_id, name="x",
            frontmatter_common={"description": _GOOD_DESC},
            frontmatter_backend={},
            files={"reference.md": SkillFile(path="reference.md", content="r")},
        )


async def test_update_with_files_missing_skill_md_is_rejected() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("upi")
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    with pytest.raises(ValueError, match="SKILL.md"):
        await update_skill(
            s.id,
            tenant_id=tenant_id,
            files={"only.md": SkillFile(path="only.md", content="x")},
        )


# ---------------------------------------------------------------------------
# DB CHECK constraints (defense in depth — application validators are
# the first line; these prove the DB rejects bad data even if a caller
# bypasses them).
# ---------------------------------------------------------------------------


async def test_db_check_rejects_invalid_skill_name() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("nch")
    pool = _get_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO skills (tenant_id, coworker_id, name, "
                "frontmatter_common, frontmatter_backend) "
                "VALUES ($1::uuid, $2::uuid, $3, '{}'::jsonb, '{}'::jsonb)",
                tenant_id,
                coworker_id,
                "1bad-leading-digit",
            )


@pytest.mark.parametrize(
    "bad_path",
    [
        "/abs",
        "./SKILL.md",
        "..",
        "a/..",
        "a/../b",
        "double//slash.md",
        "back\\slash",
        "",
    ],
)
async def test_db_check_rejects_bad_paths(bad_path: str) -> None:
    """The DB-side regex must reject the same paths the application
    validator does. Bypass the application by inserting directly with
    admin pool.
    """
    tenant_id, coworker_id = await _make_tenant_with_coworker(
        "pch-" + str(abs(hash(bad_path)))[:6]
    )
    s = await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    pool = _get_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO skill_files (skill_id, path, content) "
                "VALUES ($1::uuid, $2, $3)",
                s.id,
                bad_path,
                "x",
            )


# ---------------------------------------------------------------------------
# SECURITY DEFINER cross-tenant trigger
# ---------------------------------------------------------------------------


async def test_cross_tenant_coworker_id_rejected_by_trigger() -> None:
    """The trigger must reject a forged tenant_id mismatch even when
    the FK constraint passes (the foreign coworker really does exist
    — just in a different tenant).
    """
    tenant_a, _ = await _make_tenant_with_coworker("xta")
    tenant_b, coworker_b = await _make_tenant_with_coworker("xtb")
    pool = _get_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="different tenant"):
            await conn.execute(
                "INSERT INTO skills (tenant_id, coworker_id, name, "
                "frontmatter_common, frontmatter_backend) "
                "VALUES ($1::uuid, $2::uuid, $3, '{}'::jsonb, '{}'::jsonb)",
                tenant_a,  # tenant A says it owns this skill
                coworker_b,  # ...but the coworker belongs to tenant B
                "forged",
            )


async def test_cross_tenant_blocked_under_rls_through_app_role(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Same scenario as above, but exercised through the RLS-bound
    rolemesh_app role. The two layers (RLS WITH CHECK + SECURITY
    DEFINER trigger) must both fire against an attempted cross-tenant
    insert.
    """
    tenant_a, _ = await _make_tenant_with_coworker("rta")
    tenant_b, coworker_b = await _make_tenant_with_coworker("rtb")
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                tenant_a,
            )
            with pytest.raises(
                (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError)
            ):
                await conn.execute(
                    "INSERT INTO skills (tenant_id, coworker_id, name, "
                    "frontmatter_common, frontmatter_backend) "
                    "VALUES ($1::uuid, $2::uuid, $3, '{}'::jsonb, '{}'::jsonb)",
                    tenant_a,
                    coworker_b,
                    "forged-rls",
                )


# ---------------------------------------------------------------------------
# RLS isolation — adversarial reads + writes through the rolemesh_app role
# ---------------------------------------------------------------------------


async def test_rls_select_isolates_skills(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    tenant_a, coworker_a = await _make_tenant_with_coworker("isa")
    tenant_b, coworker_b = await _make_tenant_with_coworker("isb")
    skill_b = await create_skill(
        tenant_id=tenant_b, coworker_id=coworker_b, name="bs",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                tenant_a,
            )
            rows = await conn.fetch(
                "SELECT * FROM skills WHERE id = $1::uuid", skill_b.id
            )
    assert rows == [], "tenant A could see tenant B's skill"


async def test_rls_select_isolates_skill_files_transitively(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """skill_files has no tenant_id of its own; isolation is via the
    EXISTS subquery on skills.
    """
    tenant_a, _ = await _make_tenant_with_coworker("tisa")
    tenant_b, coworker_b = await _make_tenant_with_coworker("tisb")
    skill_b = await create_skill(
        tenant_id=tenant_b, coworker_id=coworker_b, name="b2",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="x"),
            "secret.md": SkillFile(path="secret.md", content="confidential"),
        },
    )
    async with app_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_tenant_id', $1, true)",
                tenant_a,
            )
            rows = await conn.fetch(
                "SELECT * FROM skill_files WHERE skill_id = $1::uuid",
                skill_b.id,
            )
    assert rows == []


async def test_rls_unset_guc_returns_empty(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """No tenant context = no rows. Both tables must fail closed."""
    tenant_id, coworker_id = await _make_tenant_with_coworker("ufc")
    await create_skill(
        tenant_id=tenant_id, coworker_id=coworker_id, name="closed",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    async with app_pool.acquire() as conn:
        skills_rows = await conn.fetch("SELECT * FROM skills")
        files_rows = await conn.fetch("SELECT * FROM skill_files")
    assert skills_rows == []
    assert files_rows == []


async def test_set_skill_file_returns_none_for_foreign_tenant() -> None:
    """The application-layer ``set_skill_file`` looks up the parent
    skill under tenant scope; a tenant trying to upsert a file into a
    foreign skill should get None back (not silently write into a
    cross-tenant skill).
    """
    tenant_a, _ = await _make_tenant_with_coworker("sfa")
    tenant_b, coworker_b = await _make_tenant_with_coworker("sfb")
    skill_b = await create_skill(
        tenant_id=tenant_b, coworker_id=coworker_b, name="bx",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    result = await set_skill_file(
        skill_b.id, "leak.md", tenant_id=tenant_a, content="should not appear"
    )
    assert result is None

    pool = _get_pool()
    async with pool.acquire() as conn:
        files = await conn.fetch(
            "SELECT path FROM skill_files WHERE skill_id = $1::uuid",
            skill_b.id,
        )
    assert {r["path"] for r in files} == {"SKILL.md"}, (
        "foreign-tenant set_skill_file leaked through"
    )
