"""Skills DB layer: schema constraints, RLS isolation (incl. transitive
on skill_files), CRUD happy paths, and the SKILL.md application invariant.

Runs against the testcontainer Postgres. Two pools at play:

* The default test pool connects as the testcontainer's superuser and
  bypasses RLS — used to seed cross-tenant fixtures so we can poke
  at boundaries.
* ``app_pool`` connects as ``rolemesh_app`` (NOBYPASSRLS) and is the
  only place where RLS actually fires.

Tests are deliberately adversarial: each one tries to do something
the design says must fail, and asserts it does.

v1.1 03b: ``skills`` is per-tenant; coworker association lives in
``coworker_skills``. Fixtures use ``create_skill_for_coworker`` (the
transactional convenience helper that mirrors the old call shape) so
the rest of the suite stays close to the pre-03b feel.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import asyncpg
import pytest

from rolemesh.core.types import SkillFile
from rolemesh.db import (
    _get_pool,
    create_coworker,
    create_skill,
    create_skill_for_coworker,
    create_tenant,
    delete_skill,
    delete_skill_file,
    disable_skill_for_coworker,
    enable_skill_for_coworker,
    get_skill,
    is_skill_bound_to_coworker,
    is_skill_enabled_for_coworker,
    list_skills_for_coworker,
    list_skills_for_tenant,
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
    created = await create_skill_for_coworker(
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


async def test_list_for_coworker_filters_by_enabled_flags() -> None:
    """``enabled_only`` is double-AND: both the catalog ``skills.enabled``
    and per-coworker ``coworker_skills.enabled`` must be TRUE.
    """
    tenant_id, coworker_id = await _make_tenant_with_coworker("flt")
    enabled = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="enabled-skill",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(), enabled=True,
    )
    disabled = await create_skill_for_coworker(
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


async def test_per_coworker_disable_masks_globally_enabled_skill() -> None:
    """The per-coworker ``coworker_skills.enabled=FALSE`` masks a
    catalog skill that is globally enabled. Verifies the second half
    of the double-AND — flipping only the junction flag.
    """
    tenant_id, coworker_id = await _make_tenant_with_coworker("ovr")
    skill = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="masked",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(), enabled=True,
    )
    await enable_skill_for_coworker(
        skill_id=skill.id, coworker_id=coworker_id,
        tenant_id=tenant_id, enabled=False,
    )
    only_enabled = await list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, enabled_only=True
    )
    assert only_enabled == [], (
        "coworker_skills.enabled=FALSE should mask a globally-enabled skill"
    )
    # ``enabled_only=False`` still surfaces the binding so admin
    # tooling can see it.
    all_skills = await list_skills_for_coworker(
        coworker_id, tenant_id=tenant_id, enabled_only=False
    )
    assert {s.id for s in all_skills} == {skill.id}


async def test_shared_skill_between_two_coworkers_same_tenant() -> None:
    """Per-tenant catalog: two coworkers in the same tenant can bind
    the same skill row. Pre-03b this was impossible (one row per
    coworker); the test guards against accidentally re-introducing
    that constraint.
    """
    tenant_id, cw_a = await _make_tenant_with_coworker("shr-a")
    # Second coworker in the SAME tenant.
    cw_b = (await create_coworker(
        tenant_id=tenant_id, name="CWshrB",
        folder=f"cw-shr-b-{uuid.uuid4().hex[:6]}",
    )).id

    skill = await create_skill(
        tenant_id=tenant_id,
        name="shared",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files=_basic_files(),
    )
    assert await enable_skill_for_coworker(
        skill_id=skill.id, coworker_id=cw_a, tenant_id=tenant_id,
    )
    assert await enable_skill_for_coworker(
        skill_id=skill.id, coworker_id=cw_b, tenant_id=tenant_id,
    )

    a_skills = await list_skills_for_coworker(cw_a, tenant_id=tenant_id)
    b_skills = await list_skills_for_coworker(cw_b, tenant_id=tenant_id)
    assert {s.id for s in a_skills} == {skill.id}
    assert {s.id for s in b_skills} == {skill.id}


async def test_delete_coworker_cascades_to_bindings_only() -> None:
    """Deleting a coworker drops ``coworker_skills`` rows (CASCADE)
    but leaves the catalog ``skills`` row intact — per-tenant catalog
    semantics. Verifies the cascade chain we expect after the 03b cut.
    """
    tenant_id, coworker_id = await _make_tenant_with_coworker("cwd")
    s = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="keeper",
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
        bindings_left = await conn.fetch(
            "SELECT * FROM coworker_skills WHERE skill_id = $1::uuid", s.id
        )
        files_left = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid", s.id
        )
    assert len(skills_left) == 1, "catalog skill row should remain"
    assert bindings_left == [], "coworker_skills should cascade-delete"
    assert files_left, "skill_files belong to the catalog row and should stay"


async def test_update_replaces_files_atomically() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("upd")
    s = await create_skill_for_coworker(
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
    s = await create_skill_for_coworker(
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
    s = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    with pytest.raises(ValueError, match="SKILL.md cannot be deleted"):
        await delete_skill_file(s.id, "SKILL.md", tenant_id=tenant_id)


async def test_delete_skill_file_returns_false_for_unknown() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("rfn")
    s = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    assert await delete_skill_file(s.id, "nope.md", tenant_id=tenant_id) is False


async def test_delete_skill_cascades_to_files() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("dcs")
    s = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="x",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="b"),
            "ref.md": SkillFile(path="ref.md", content="r"),
        },
    )
    assert await delete_skill(s.id, tenant_id=tenant_id) is True
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid", s.id
        )
    assert rows == []


# ---------------------------------------------------------------------------
# SKILL.md application invariant
# ---------------------------------------------------------------------------


async def test_create_without_skill_md_is_rejected() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("inv")
    with pytest.raises(ValueError, match="SKILL.md"):
        await create_skill_for_coworker(
            tenant_id=tenant_id, coworker_id=coworker_id, name="x",
            frontmatter_common={"description": _GOOD_DESC},
            frontmatter_backend={},
            files={"reference.md": SkillFile(path="reference.md", content="r")},
        )


async def test_update_with_files_missing_skill_md_is_rejected() -> None:
    tenant_id, coworker_id = await _make_tenant_with_coworker("upi")
    s = await create_skill_for_coworker(
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


@pytest.mark.parametrize(
    "bad_name",
    [
        "HasUpper",          # uppercase forbidden under lowercase-kebab regex
        "has_underscore",    # underscore forbidden
        "has space",         # whitespace forbidden
        "a" * 65,            # over the 64-char cap
        "anthropic",         # reserved (NOT IN clause)
        "claude",            # reserved (NOT IN clause)
    ],
)
async def test_db_check_rejects_invalid_skill_name(bad_name: str) -> None:
    # DB-side defense-in-depth: even when callers bypass the
    # application validator (validate_skill_name in core/skills.py)
    # and Pydantic, the CHECK constraint must still reject. The
    # parametrize covers both branches — the regex and the reserved
    # NOT IN list — so a future schema migration that loses one of
    # them gets caught.
    tenant_id, _ = await _make_tenant_with_coworker(
        "nch-" + str(abs(hash(bad_name)))[:6]
    )
    pool = _get_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO skills (tenant_id, name, "
                "frontmatter_common, frontmatter_backend) "
                "VALUES ($1::uuid, $2, '{}'::jsonb, '{}'::jsonb)",
                tenant_id,
                bad_name,
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
    s = await create_skill_for_coworker(
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
# coworker_skills SECURITY DEFINER cross-tenant trigger
# ---------------------------------------------------------------------------


async def test_coworker_skills_rejects_cross_tenant_binding() -> None:
    """Skill in tenant A + coworker in tenant B must not be bindable
    even via the bypass-RLS admin pool. The SECURITY DEFINER trigger
    on ``coworker_skills`` enforces this — the replacement for the
    pre-03b ``skills_check_coworker_tenant`` trigger.
    """
    tenant_a, _ = await _make_tenant_with_coworker("xta")
    _tenant_b, coworker_b = await _make_tenant_with_coworker("xtb")
    skill_a = await create_skill(
        tenant_id=tenant_a, name="forged-bind",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    pool = _get_pool()
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.RaiseError, match="tenant mismatch"):
            await conn.execute(
                "INSERT INTO coworker_skills (coworker_id, skill_id, enabled) "
                "VALUES ($1::uuid, $2::uuid, TRUE)",
                coworker_b, skill_a.id,
            )


async def test_enable_skill_for_coworker_rejects_foreign_tenant() -> None:
    """The application-layer helper refuses cross-tenant bindings —
    returns ``False`` rather than letting the DB trigger fire and
    blow up.
    """
    tenant_a, _ = await _make_tenant_with_coworker("eta")
    _tenant_b, coworker_b = await _make_tenant_with_coworker("etb")
    skill_a = await create_skill(
        tenant_id=tenant_a, name="cross",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    # Same tenant_id on both sides of the call, but the coworker is in
    # a different tenant — the EXISTS guard catches it.
    ok = await enable_skill_for_coworker(
        skill_id=skill_a.id, coworker_id=coworker_b, tenant_id=tenant_a,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# RLS isolation — adversarial reads + writes through the rolemesh_app role
# ---------------------------------------------------------------------------


async def test_rls_select_isolates_skills(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    tenant_a, _ = await _make_tenant_with_coworker("isa")
    tenant_b, coworker_b = await _make_tenant_with_coworker("isb")
    skill_b = await create_skill_for_coworker(
        tenant_id=tenant_b, coworker_id=coworker_b, name="bs",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    async with app_pool.acquire() as conn, conn.transaction():
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
    skill_b = await create_skill_for_coworker(
        tenant_id=tenant_b, coworker_id=coworker_b, name="b2",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={},
        files={
            "SKILL.md": SkillFile(path="SKILL.md", content="x"),
            "secret.md": SkillFile(path="secret.md", content="confidential"),
        },
    )
    async with app_pool.acquire() as conn, conn.transaction():
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
    await create_skill_for_coworker(
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
    skill_b = await create_skill_for_coworker(
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


# ---------------------------------------------------------------------------
# Per-tenant catalog reader
# ---------------------------------------------------------------------------


async def test_list_skills_for_tenant_returns_unbound_rows_too() -> None:
    """``list_skills_for_tenant`` is the v1 flat reader — it must
    return catalog rows even when no coworker binding exists yet
    (the create-then-bind flow).
    """
    tenant_id, _ = await _make_tenant_with_coworker("flat")
    s1 = await create_skill(
        tenant_id=tenant_id, name="a",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    s2 = await create_skill(
        tenant_id=tenant_id, name="b",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    rows = await list_skills_for_tenant(tenant_id)
    ids = {s.id for s in rows}
    assert {s1.id, s2.id} <= ids


# ---------------------------------------------------------------------------
# Binding helpers — bind/enabled state
# ---------------------------------------------------------------------------


async def test_is_skill_bound_vs_enabled_helpers() -> None:
    """``bound_to`` is looser than ``enabled_for``: disabling either
    flag flips the latter but not the former.
    """
    tenant_id, coworker_id = await _make_tenant_with_coworker("hlp")
    skill = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="h",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    assert await is_skill_bound_to_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )
    assert await is_skill_enabled_for_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )

    # Disable on the catalog side.
    updated = await update_skill(skill.id, tenant_id=tenant_id, enabled=False)
    assert updated is not None and updated.enabled is False
    assert await is_skill_bound_to_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )
    assert not await is_skill_enabled_for_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )

    # Re-enable globally; disable per-coworker.
    await update_skill(skill.id, tenant_id=tenant_id, enabled=True)
    await enable_skill_for_coworker(
        skill_id=skill.id, coworker_id=coworker_id,
        tenant_id=tenant_id, enabled=False,
    )
    assert await is_skill_bound_to_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )
    assert not await is_skill_enabled_for_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )


async def test_disable_removes_binding_row() -> None:
    """``disable_skill_for_coworker`` deletes the binding (the v1 DELETE
    semantic), distinct from setting ``enabled=False`` which keeps it.
    """
    tenant_id, coworker_id = await _make_tenant_with_coworker("dis")
    skill = await create_skill_for_coworker(
        tenant_id=tenant_id, coworker_id=coworker_id, name="d",
        frontmatter_common={"description": _GOOD_DESC},
        frontmatter_backend={}, files=_basic_files(),
    )
    removed = await disable_skill_for_coworker(
        skill_id=skill.id, coworker_id=coworker_id, tenant_id=tenant_id,
    )
    assert removed is True
    assert not await is_skill_bound_to_coworker(
        skill.id, coworker_id, tenant_id=tenant_id
    )
    # Catalog row stays.
    assert await get_skill(skill.id, tenant_id=tenant_id) is not None

    # Idempotent.
    again = await disable_skill_for_coworker(
        skill_id=skill.id, coworker_id=coworker_id, tenant_id=tenant_id,
    )
    assert again is False
