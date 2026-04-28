"""coworker_config freeze tests.

Cover the contract:
  * canonical hashing — re-freezing produces the same sha256
  * order-independence — skill files / tools sorted before hashing
  * skills tables missing → empty list, no crash (parallel-worktree
    safety)
  * unknown coworker → LookupError (RLS reduces wrong-tenant to
    not-found, which the CLI surfaces)
"""

from __future__ import annotations

import json
import uuid

import pytest

from rolemesh.db.pg import (
    admin_conn,
    create_coworker,
    create_tenant,
)
from rolemesh.evaluation.freeze import freeze_coworker_config

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_skill(
    *, tenant_id: str, coworker_id: str, name: str,
    files: dict[str, str], frontmatter_common: dict | None = None,
    enabled: bool = True,
) -> str:
    async with admin_conn() as conn:
        skill_id = await conn.fetchval(
            """
            INSERT INTO skills (
                tenant_id, coworker_id, name,
                frontmatter_common, frontmatter_backend, enabled
            )
            VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, '{}'::jsonb, $5)
            RETURNING id
            """,
            tenant_id, coworker_id, name,
            json.dumps(frontmatter_common or {}), enabled,
        )
        for path, content in files.items():
            await conn.execute(
                """
                INSERT INTO skill_files (skill_id, path, content)
                VALUES ($1::uuid, $2, $3)
                """,
                skill_id, path, content,
            )
    return str(skill_id)


# ---------------------------------------------------------------------------
# Without skills table present
# ---------------------------------------------------------------------------


async def test_freeze_works_without_skills_tables() -> None:
    """Parallel worktree hasn't landed yet: freeze must not crash,
    and ``skills`` should appear as an empty list in the output."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
        system_prompt="hi",
    )
    frozen = await freeze_coworker_config(cw.id, tenant_id=t.id)
    assert frozen.config["skills"] == []
    assert frozen.config["system_prompt"] == "hi"
    assert len(frozen.sha256) == 64  # sha256 hex digest


# ---------------------------------------------------------------------------
# With skills table present (skills_schema fixture)
# ---------------------------------------------------------------------------


async def test_freeze_includes_enabled_skill_files(skills_schema: None) -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="alpha",
        files={"SKILL.md": "alpha body", "ref/notes.md": "beta"},
        frontmatter_common={"description": "alpha skill"},
    )
    frozen = await freeze_coworker_config(cw.id, tenant_id=t.id)
    skills = frozen.config["skills"]
    assert len(skills) == 1
    assert skills[0]["name"] == "alpha"
    # Files dict carries full contents — eval reproducibility hinges
    # on this; a name-only snapshot would let a silent skill edit go
    # unnoticed across runs.
    assert skills[0]["files"]["SKILL.md"] == "alpha body"
    assert skills[0]["files"]["ref/notes.md"] == "beta"


async def test_freeze_skips_disabled_skills(skills_schema: None) -> None:
    """Disabled skills are not bind-mounted into containers, so they
    must not appear in the snapshot either — including them would
    inflate the config sha and falsely diff against live behaviour."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="enabled-one",
        files={"SKILL.md": "x"},
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="disabled-one",
        files={"SKILL.md": "y"}, enabled=False,
    )
    frozen = await freeze_coworker_config(cw.id, tenant_id=t.id)
    names = [s["name"] for s in frozen.config["skills"]]
    assert names == ["enabled-one"]


async def test_freeze_hash_stable_across_calls(skills_schema: None) -> None:
    """Repeated freeze of unchanged coworker must produce identical
    sha — config-clustering depends on this. Mutation guard: sort_keys
    in canonical dump."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="s1",
        files={"a.md": "1", "b.md": "2"},
    )
    f1 = await freeze_coworker_config(cw.id, tenant_id=t.id)
    f2 = await freeze_coworker_config(cw.id, tenant_id=t.id)
    assert f1.sha256 == f2.sha256
    assert f1.config == f2.config


async def test_freeze_hash_changes_when_skill_edits(skills_schema: None) -> None:
    """A single byte change in a skill file body must move the hash —
    otherwise eval can't tell two runs apart by config."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    skill_id = await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="s1",
        files={"SKILL.md": "v1"},
    )
    h1 = (await freeze_coworker_config(cw.id, tenant_id=t.id)).sha256

    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE skill_files SET content = 'v2' "
            "WHERE skill_id = $1::uuid AND path = 'SKILL.md'",
            skill_id,
        )
    h2 = (await freeze_coworker_config(cw.id, tenant_id=t.id)).sha256
    assert h1 != h2


async def test_freeze_unknown_coworker_raises_lookup_error() -> None:
    """RLS reduces wrong-tenant to not-found; the CLI relies on this
    to surface a clear error rather than silently freezing nothing."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    bogus_id = str(uuid.uuid4())
    with pytest.raises(LookupError):
        await freeze_coworker_config(bogus_id, tenant_id=t.id)


async def test_freeze_skill_file_order_deterministic(skills_schema: None) -> None:
    """Files are inserted in inverse alpha order; freeze must still
    list them sorted so repeat freezes hash identically."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    skill_id = uuid.uuid4()
    async with admin_conn() as conn:
        await conn.execute(
            """
            INSERT INTO skills (id, tenant_id, coworker_id, name, enabled)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'order-skill', TRUE)
            """,
            str(skill_id), t.id, cw.id,
        )
        # Insert in reverse-alpha so the underlying scan order is bad.
        for path, content in [("z.md", "z"), ("m.md", "m"), ("a.md", "a")]:
            await conn.execute(
                "INSERT INTO skill_files (skill_id, path, content) "
                "VALUES ($1::uuid, $2, $3)",
                str(skill_id), path, content,
            )

    h1 = (await freeze_coworker_config(cw.id, tenant_id=t.id)).sha256
    h2 = (await freeze_coworker_config(cw.id, tenant_id=t.id)).sha256
    assert h1 == h2


# ---------------------------------------------------------------------------
# Schema-conformance smoke tests — guard against fixture drift from the
# real ``feat/skills`` schema. If the strict fixture ever weakens (e.g.
# someone drops a CHECK), these tests catch it before the real schema
# rejects production-bound data.
# ---------------------------------------------------------------------------


async def test_seed_rejects_invalid_skill_name(skills_schema: None) -> None:
    """The real ``skills`` table CHECKs ``name ~ '^[a-zA-Z][a-zA-Z0-9_-]{0,63}$'``;
    an underscore-prefixed name should be rejected. If the fixture drops
    the regex CHECK, this test fails loud."""
    import asyncpg

    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with admin_conn() as conn:
            await conn.execute(
                """
                INSERT INTO skills (tenant_id, coworker_id, name, enabled)
                VALUES ($1::uuid, $2::uuid, '_bad_name', TRUE)
                """,
                t.id, cw.id,
            )


async def test_seed_rejects_skill_file_path_with_dot_segment(
    skills_schema: None,
) -> None:
    """Path CHECK forbids ``../`` traversal segments. Ensures the fixture
    enforces it so freeze tests can't silently rely on paths the real
    schema would reject."""
    import asyncpg

    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    skill_id = await _seed_skill(
        tenant_id=t.id, coworker_id=cw.id, name="ok",
        files={"SKILL.md": "ok"},
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with admin_conn() as conn:
            await conn.execute(
                """
                INSERT INTO skill_files (skill_id, path, content)
                VALUES ($1::uuid, '../etc/passwd', 'evil')
                """,
                skill_id,
            )
