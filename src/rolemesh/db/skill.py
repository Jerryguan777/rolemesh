"""Skills (per-coworker capability folders) + skill files."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.core.types import Skill, SkillFile
from rolemesh.db._pool import tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "create_skill",
    "delete_skill",
    "delete_skill_file",
    "get_skill",
    "list_skills_for_coworker",
    "set_skill_file",
    "update_skill",
]


# ---------------------------------------------------------------------------
# Skills (per-coworker capability folders)
# ---------------------------------------------------------------------------


def _record_to_skill(row: asyncpg.Record, files: list[SkillFile] | None = None) -> Skill:
    """Build a ``Skill`` dataclass from a ``skills`` row.

    ``files`` is optional — set it when the caller has already
    fetched ``skill_files`` for this skill. Otherwise the dataclass
    has an empty ``files`` map; callers that only need metadata can
    skip the second query.
    """
    fc_raw = row["frontmatter_common"]
    fb_raw = row["frontmatter_backend"]
    fc = json.loads(fc_raw) if isinstance(fc_raw, str) else (fc_raw or {})
    fb = json.loads(fb_raw) if isinstance(fb_raw, str) else (fb_raw or {})
    return Skill(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        name=row["name"],
        frontmatter_common=fc,
        frontmatter_backend=fb,
        enabled=bool(row["enabled"]),
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        created_by=str(row["created_by"]) if row["created_by"] else None,
        files={f.path: f for f in (files or [])},
    )


def _record_to_skill_file(row: asyncpg.Record) -> SkillFile:
    return SkillFile(
        path=row["path"],
        content=row["content"],
        mime_type=row["mime_type"],
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def create_skill(
    *,
    tenant_id: str,
    coworker_id: str,
    name: str,
    frontmatter_common: dict[str, Any],
    frontmatter_backend: dict[str, dict[str, Any]],
    files: dict[str, SkillFile],
    enabled: bool = True,
    created_by: str | None = None,
) -> Skill:
    """Create a skill plus its files in a single transaction.

    Caller is expected to have run the frontmatter splitter and
    ``validate_skill_*`` helpers from ``rolemesh.core.skills``
    already; the DB CHECK constraints and SECURITY DEFINER trigger
    are the second line of defense.

    The ``files`` map must contain ``SKILL.md`` (the application
    invariant). The DB does not enforce this directly because we
    cannot express "every skill has a row with path = 'SKILL.md'"
    as a single CHECK; the application enforces it on every write.
    """
    if "SKILL.md" not in files:
        raise ValueError("skill files must contain SKILL.md")
    fc_json = json.dumps(frontmatter_common)
    fb_json = json.dumps(frontmatter_backend)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO skills (tenant_id, coworker_id, name,
                                frontmatter_common, frontmatter_backend,
                                enabled, created_by)
            VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, $5::jsonb, $6,
                    $7::uuid)
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            name,
            fc_json,
            fb_json,
            enabled,
            created_by,
        )
        assert row is not None
        skill_id = row["id"]
        # Insert all files in a single batch
        await conn.executemany(
            """
            INSERT INTO skill_files (skill_id, path, content, mime_type)
            VALUES ($1::uuid, $2, $3, $4)
            """,
            [
                (skill_id, f.path, f.content, f.mime_type)
                for f in files.values()
            ],
        )
        # Fetch back files (canonical updated_at from server clock)
        file_rows = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid ORDER BY path",
            skill_id,
        )
    return _record_to_skill(row, [_record_to_skill_file(r) for r in file_rows])


async def get_skill(
    skill_id: str, *, tenant_id: str, with_files: bool = True
) -> Skill | None:
    """Fetch a skill by id, scoped to ``tenant_id``.

    The explicit ``AND tenant_id`` is the application-layer defense
    in depth alongside RLS, matching the convention used elsewhere
    in this module (see ``get_coworker``).
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM skills WHERE id = $1::uuid AND tenant_id = $2::uuid",
            skill_id,
            tenant_id,
        )
        if row is None:
            return None
        files: list[SkillFile] = []
        if with_files:
            file_rows = await conn.fetch(
                "SELECT * FROM skill_files WHERE skill_id = $1::uuid ORDER BY path",
                skill_id,
            )
            files = [_record_to_skill_file(r) for r in file_rows]
    return _record_to_skill(row, files)


async def list_skills_for_coworker(
    coworker_id: str,
    *,
    tenant_id: str,
    enabled_only: bool = False,
    with_files: bool = False,
) -> list[Skill]:
    """List all skills for a coworker.

    ``enabled_only`` filters to projection-eligible skills (used by
    the orchestrator at spawn time). ``with_files`` controls whether
    the ``files`` map is populated — admin REST list responses skip
    file content; the projector needs it.
    """
    async with tenant_conn(tenant_id) as conn:
        if enabled_only:
            rows = await conn.fetch(
                "SELECT * FROM skills WHERE coworker_id = $1::uuid "
                "AND tenant_id = $2::uuid AND enabled = TRUE ORDER BY name",
                coworker_id,
                tenant_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM skills WHERE coworker_id = $1::uuid "
                "AND tenant_id = $2::uuid ORDER BY name",
                coworker_id,
                tenant_id,
            )
        result: list[Skill] = []
        if with_files and rows:
            ids = [r["id"] for r in rows]
            file_rows = await conn.fetch(
                "SELECT * FROM skill_files WHERE skill_id = ANY($1::uuid[]) "
                "ORDER BY skill_id, path",
                ids,
            )
            files_by_skill: dict[str, list[SkillFile]] = {}
            for fr in file_rows:
                files_by_skill.setdefault(str(fr["skill_id"]), []).append(
                    _record_to_skill_file(fr)
                )
            for r in rows:
                result.append(
                    _record_to_skill(r, files_by_skill.get(str(r["id"]), []))
                )
        else:
            for r in rows:
                result.append(_record_to_skill(r))
    return result


async def update_skill(
    skill_id: str,
    *,
    tenant_id: str,
    frontmatter_common: dict[str, Any] | None = None,
    frontmatter_backend: dict[str, dict[str, Any]] | None = None,
    enabled: bool | None = None,
    files: dict[str, SkillFile] | None = None,
) -> Skill | None:
    """Update a skill's frontmatter, enabled flag, and/or files.

    Files semantics: if ``files`` is provided, it is treated as a
    full replacement of the skill's file set. SKILL.md must be
    present in the new map. Use ``set_skill_file`` /
    ``delete_skill_file`` for surgical edits.
    """
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1
    if frontmatter_common is not None:
        fields.append(f"frontmatter_common = ${param_idx}::jsonb")
        values.append(json.dumps(frontmatter_common))
        param_idx += 1
    if frontmatter_backend is not None:
        fields.append(f"frontmatter_backend = ${param_idx}::jsonb")
        values.append(json.dumps(frontmatter_backend))
        param_idx += 1
    if enabled is not None:
        fields.append(f"enabled = ${param_idx}")
        values.append(enabled)
        param_idx += 1

    if files is not None and "SKILL.md" not in files:
        raise ValueError("skill files must contain SKILL.md")

    async with tenant_conn(tenant_id) as conn:
        if fields:
            fields.append("updated_at = now()")
            values.append(skill_id)
            tenant_param = param_idx + 1
            values.append(tenant_id)
            sql = (
                f"UPDATE skills SET {', '.join(fields)} "
                f"WHERE id = ${param_idx}::uuid AND tenant_id = ${tenant_param}::uuid "
                f"RETURNING *"
            )
            row = await conn.fetchrow(sql, *values)
        else:
            row = await conn.fetchrow(
                "SELECT * FROM skills WHERE id = $1::uuid "
                "AND tenant_id = $2::uuid",
                skill_id,
                tenant_id,
            )
        if row is None:
            return None
        if files is not None:
            await conn.execute(
                "DELETE FROM skill_files WHERE skill_id = $1::uuid", skill_id
            )
            await conn.executemany(
                """
                INSERT INTO skill_files (skill_id, path, content, mime_type)
                VALUES ($1::uuid, $2, $3, $4)
                """,
                [
                    (skill_id, f.path, f.content, f.mime_type)
                    for f in files.values()
                ],
            )
            # Touch updated_at so list views reflect file changes too,
            # even if no metadata fields changed.
            row = await conn.fetchrow(
                "UPDATE skills SET updated_at = now() WHERE id = $1::uuid "
                "AND tenant_id = $2::uuid RETURNING *",
                skill_id,
                tenant_id,
            )
            assert row is not None
        file_rows = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid ORDER BY path",
            skill_id,
        )
    return _record_to_skill(row, [_record_to_skill_file(r) for r in file_rows])


async def delete_skill(skill_id: str, *, tenant_id: str) -> bool:
    """Hard-delete a skill (cascades to skill_files). Returns True if a
    row was actually deleted, False if the skill did not exist for this
    tenant.
    """
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM skills WHERE id = $1::uuid AND tenant_id = $2::uuid",
            skill_id,
            tenant_id,
        )
    # asyncpg returns "DELETE n" — parse the count
    return result.endswith(" 1")


async def set_skill_file(
    skill_id: str,
    path: str,
    *,
    tenant_id: str,
    content: str,
    mime_type: str = "text/plain",
) -> SkillFile | None:
    """Upsert a single file in a skill. Returns the new ``SkillFile`` or
    ``None`` if the parent skill does not belong to ``tenant_id``.

    The application-layer SKILL.md invariant is enforced by the REST
    layer, not here — so callers can use this for both fresh adds and
    edits. The DB's CHECK on ``path`` rejects traversal.
    """
    async with tenant_conn(tenant_id) as conn:
        # Verify the parent skill belongs to this tenant before inserting.
        # Explicit AND tenant_id is the application-layer mirror of the
        # RLS policy — the test pool runs as superuser and bypasses RLS,
        # so without this clause cross-tenant attempts would slip through
        # in tests even though they're blocked in production.
        exists = await conn.fetchval(
            "SELECT 1 FROM skills WHERE id = $1::uuid AND tenant_id = $2::uuid",
            skill_id,
            tenant_id,
        )
        if not exists:
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO skill_files (skill_id, path, content, mime_type)
            VALUES ($1::uuid, $2, $3, $4)
            ON CONFLICT (skill_id, path) DO UPDATE
                SET content = EXCLUDED.content,
                    mime_type = EXCLUDED.mime_type,
                    updated_at = now()
            RETURNING *
            """,
            skill_id,
            path,
            content,
            mime_type,
        )
        await conn.execute(
            "UPDATE skills SET updated_at = now() WHERE id = $1::uuid "
            "AND tenant_id = $2::uuid",
            skill_id,
            tenant_id,
        )
    assert row is not None
    return _record_to_skill_file(row)


async def delete_skill_file(
    skill_id: str, path: str, *, tenant_id: str
) -> bool:
    """Remove a single file from a skill.

    Refuses to delete ``SKILL.md`` — that is the application-layer
    invariant. Returns True if a row was actually deleted.
    """
    if path == "SKILL.md":
        raise ValueError("SKILL.md cannot be deleted from a skill")
    async with tenant_conn(tenant_id) as conn:
        # Defense in depth: verify the parent skill belongs to this
        # tenant before deleting (the EXISTS subquery on skills carries
        # the tenant_id check). RLS on skill_files would reject a
        # cross-tenant delete in production, but the test pool runs as
        # superuser — explicit AND tenant_id closes that gap.
        result = await conn.execute(
            "DELETE FROM skill_files USING skills "
            "WHERE skill_files.skill_id = $1::uuid AND skill_files.path = $2 "
            "AND skills.id = skill_files.skill_id "
            "AND skills.tenant_id = $3::uuid",
            skill_id,
            path,
            tenant_id,
        )
        if result.endswith(" 1"):
            await conn.execute(
                "UPDATE skills SET updated_at = now() WHERE id = $1::uuid "
                "AND tenant_id = $2::uuid",
                skill_id,
                tenant_id,
            )
            return True
    return False
