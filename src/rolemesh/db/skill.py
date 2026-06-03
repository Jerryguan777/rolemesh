"""Skills (per-tenant capability catalog) + skill files + coworker bindings.

v1.1 03b made the catalog per-tenant: a single ``skills`` row may now
be bound to any number of coworkers in the same tenant via the
``coworker_skills`` junction. Reader/writer surface follows that:

* ``create_skill`` only takes ``tenant_id`` and inserts into the
  catalog. Bind separately with ``enable_skill_for_coworker``.
* ``list_skills_for_coworker`` JOINs ``coworker_skills`` with the
  double-``enabled`` AND: ``coworker_skills.enabled``
  (per-coworker override) AND ``skills.enabled`` (global flag).
* ``create_skill_for_coworker`` is the convenience helper for code
  paths that want the old "make + bind" shape in one transaction
  (admin REST, fixture helpers). It mirrors the 02b
  ``replace_coworker_mcp_configs`` strategy of keeping a transactional
  helper alongside the lower-level primitives.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.core.skills_consts_pin import SKILL_MANIFEST_NAME
from rolemesh.core.types import Skill, SkillFile
from rolemesh.db._pool import tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "create_skill",
    "create_skill_for_coworker",
    "delete_skill",
    "delete_skill_file",
    "disable_skill_for_coworker",
    "enable_skill_for_coworker",
    "get_skill",
    "is_skill_bound_to_coworker",
    "is_skill_enabled_for_coworker",
    "list_coworkers_for_skill",
    "list_skills_for_coworker",
    "list_skills_for_tenant",
    "set_skill_file",
    "update_skill",
]


# ---------------------------------------------------------------------------
# Catalog row -> dataclass
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
        name=row["name"],
        frontmatter_common=fc,
        frontmatter_backend=fb,
        enabled=bool(row["enabled"]),
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        created_by_user_id=(
            str(row["created_by_user_id"])
            if row["created_by_user_id"] else None
        ),
        files={f.path: f for f in (files or [])},
    )


def _record_to_skill_file(row: asyncpg.Record) -> SkillFile:
    return SkillFile(
        path=row["path"],
        content=row["content"],
        mime_type=row["mime_type"],
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


# ---------------------------------------------------------------------------
# Catalog CRUD
# ---------------------------------------------------------------------------


async def create_skill(
    *,
    tenant_id: str,
    name: str,
    frontmatter_common: dict[str, Any],
    frontmatter_backend: dict[str, dict[str, Any]],
    files: dict[str, SkillFile],
    enabled: bool = True,
    created_by_user_id: str | None = None,
) -> Skill:
    """Create a per-tenant skill plus its files in a single transaction.

    Caller is expected to have run the frontmatter splitter and
    ``validate_skill_*`` helpers from ``rolemesh.core.skills``
    already; the DB CHECK constraints are the second line of defense.

    The ``files`` map must contain ``SKILL.md`` (the application
    invariant). The DB does not enforce this directly because we
    cannot express "every skill has a row with path = 'SKILL.md'"
    as a single CHECK; the application enforces it on every write.

    Coworker binding is separate — use ``enable_skill_for_coworker``
    or the transactional convenience helper
    :func:`create_skill_for_coworker`.
    """
    if SKILL_MANIFEST_NAME not in files:
        raise ValueError(f"skill files must contain {SKILL_MANIFEST_NAME}")
    fc_json = json.dumps(frontmatter_common)
    fb_json = json.dumps(frontmatter_backend)
    async with tenant_conn(tenant_id) as conn, conn.transaction():
        row = await conn.fetchrow(
            """
                INSERT INTO skills (tenant_id, name,
                                    frontmatter_common, frontmatter_backend,
                                    enabled, created_by_user_id)
                VALUES ($1::uuid, $2, $3::jsonb, $4::jsonb, $5, $6::uuid)
                RETURNING *
                """,
            tenant_id,
            name,
            fc_json,
            fb_json,
            enabled,
            created_by_user_id,
        )
        assert row is not None
        skill_id = row["id"]
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
        file_rows = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid ORDER BY path",
            skill_id,
        )
    return _record_to_skill(row, [_record_to_skill_file(r) for r in file_rows])


async def create_skill_for_coworker(
    *,
    tenant_id: str,
    coworker_id: str,
    name: str,
    frontmatter_common: dict[str, Any],
    frontmatter_backend: dict[str, dict[str, Any]],
    files: dict[str, SkillFile],
    enabled: bool = True,
    created_by_user_id: str | None = None,
) -> Skill:
    """Transactional convenience: catalog create + coworker bind.

    Mirrors the 02b ``replace_coworker_mcp_configs`` pattern: keeps
    a single-call shape for callers that conceptually own both rows
    (admin REST endpoints that grew up before the catalog/junction
    split, and test fixtures). Wraps both INSERTs in a single
    ``conn.transaction()`` so a half-state — skill row with no
    binding — is invisible to other readers.

    Returns the ``Skill`` dataclass for the freshly-created catalog
    row. The binding (``coworker_skills.enabled = TRUE``) is implicit
    and not surfaced on the return value.
    """
    if SKILL_MANIFEST_NAME not in files:
        raise ValueError(f"skill files must contain {SKILL_MANIFEST_NAME}")
    fc_json = json.dumps(frontmatter_common)
    fb_json = json.dumps(frontmatter_backend)
    async with tenant_conn(tenant_id) as conn, conn.transaction():
        row = await conn.fetchrow(
            """
                INSERT INTO skills (tenant_id, name,
                                    frontmatter_common, frontmatter_backend,
                                    enabled, created_by_user_id)
                VALUES ($1::uuid, $2, $3::jsonb, $4::jsonb, $5, $6::uuid)
                RETURNING *
                """,
            tenant_id,
            name,
            fc_json,
            fb_json,
            enabled,
            created_by_user_id,
        )
        assert row is not None
        skill_id = row["id"]
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
        await conn.execute(
            """
                INSERT INTO coworker_skills (coworker_id, skill_id, enabled)
                VALUES ($1::uuid, $2::uuid, TRUE)
                """,
            coworker_id,
            skill_id,
        )
        file_rows = await conn.fetch(
            "SELECT * FROM skill_files WHERE skill_id = $1::uuid ORDER BY path",
            skill_id,
        )
    return _record_to_skill(row, [_record_to_skill_file(r) for r in file_rows])


async def get_skill(
    skill_id: str, *, tenant_id: str, with_files: bool = True
) -> Skill | None:
    """Fetch a catalog skill by id, scoped to ``tenant_id``.

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


async def list_skills_for_tenant(
    tenant_id: str,
    *,
    with_files: bool = False,
) -> list[Skill]:
    """List every catalog skill for ``tenant_id``.

    Backs the v1 flat ``GET /api/v1/skills``. ``with_files`` defaults
    False because the list shape drops body content; the per-skill
    detail endpoint passes True.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM skills WHERE tenant_id = $1::uuid ORDER BY name",
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


async def list_skills_for_coworker(
    coworker_id: str,
    *,
    tenant_id: str,
    enabled_only: bool = False,
    with_files: bool = False,
) -> list[Skill]:
    """List skills bound to ``coworker_id`` via ``coworker_skills``.

    ``enabled_only`` filters to projection-eligible skills — both the
    per-coworker ``coworker_skills.enabled`` flag AND the global
    ``skills.enabled`` flag must be true. Missing either gate is the
    classic "I disabled this skill but it still mounted" footgun;
    the double-AND avoids it.

    ``with_files`` controls whether the ``files`` map is populated
    (admin/REST list responses skip file content; the spawn-time
    projector needs it).
    """
    async with tenant_conn(tenant_id) as conn:
        if enabled_only:
            rows = await conn.fetch(
                """
                SELECT s.*
                FROM skills s
                JOIN coworker_skills cs ON cs.skill_id = s.id
                WHERE cs.coworker_id = $1::uuid
                  AND s.tenant_id = $2::uuid
                  AND cs.enabled = TRUE
                  AND s.enabled = TRUE
                ORDER BY s.name
                """,
                coworker_id,
                tenant_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT s.*
                FROM skills s
                JOIN coworker_skills cs ON cs.skill_id = s.id
                WHERE cs.coworker_id = $1::uuid
                  AND s.tenant_id = $2::uuid
                ORDER BY s.name
                """,
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

    if files is not None and SKILL_MANIFEST_NAME not in files:
        raise ValueError(f"skill files must contain {SKILL_MANIFEST_NAME}")

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
    """Hard-delete a catalog skill (cascades to skill_files and
    coworker_skills). Returns True if a row was actually deleted,
    False if the skill did not exist for this tenant.
    """
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM skills WHERE id = $1::uuid AND tenant_id = $2::uuid",
            skill_id,
            tenant_id,
        )
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
    """
    async with tenant_conn(tenant_id) as conn:
        # Verify the parent skill belongs to this tenant before inserting.
        # Explicit AND tenant_id mirrors the RLS policy — the test pool
        # runs as superuser and bypasses RLS, so without this clause
        # cross-tenant attempts would slip through in tests.
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

    Refuses to delete ``SKILL.md`` — application-layer invariant.
    Returns True if a row was actually deleted.
    """
    if path == SKILL_MANIFEST_NAME:
        raise ValueError(f"{SKILL_MANIFEST_NAME} cannot be deleted from a skill")
    async with tenant_conn(tenant_id) as conn:
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


# ---------------------------------------------------------------------------
# Coworker <-> skill relation layer (``coworker_skills`` junction)
# ---------------------------------------------------------------------------


async def enable_skill_for_coworker(
    *,
    skill_id: str,
    coworker_id: str,
    tenant_id: str,
    enabled: bool = True,
) -> bool:
    """Bind a catalog skill to a coworker (or update the enabled flag).

    Idempotent: a repeat call with the same enabled value is a no-op
    (``ON CONFLICT DO UPDATE``). The tenant check is the same
    belt-and-braces pattern used elsewhere — both parent rows must
    exist under ``tenant_id`` before we insert, otherwise the
    coworker_skills SECURITY DEFINER trigger fires.

    Returns ``True`` when the parent rows existed and the binding is
    now in the target state; ``False`` when the parent skill or
    coworker is foreign / missing for this tenant.
    """
    async with tenant_conn(tenant_id) as conn:
        # Both parents must be in this tenant. RLS already enforces
        # this transitively for the rolemesh_app role, but tests and
        # admin paths run on the bypass-RLS pool — explicit checks
        # make the error mode "False" instead of "constraint failure".
        parents_ok = await conn.fetchval(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM skills
                    WHERE id = $1::uuid AND tenant_id = $3::uuid
                )
                AND EXISTS (
                    SELECT 1 FROM coworkers
                    WHERE id = $2::uuid AND tenant_id = $3::uuid
                )
            """,
            skill_id,
            coworker_id,
            tenant_id,
        )
        if not parents_ok:
            return False
        await conn.execute(
            """
            INSERT INTO coworker_skills (coworker_id, skill_id, enabled)
            VALUES ($1::uuid, $2::uuid, $3)
            ON CONFLICT (coworker_id, skill_id) DO UPDATE
                SET enabled = EXCLUDED.enabled
            """,
            coworker_id,
            skill_id,
            enabled,
        )
    return True


async def disable_skill_for_coworker(
    *,
    skill_id: str,
    coworker_id: str,
    tenant_id: str,
) -> bool:
    """Remove the binding row. Idempotent — returns ``False`` only when
    no row existed for ``(coworker_id, skill_id)`` under ``tenant_id``.

    Distinct from ``enable_skill_for_coworker(..., enabled=False)``:
    that keeps a row with the disabled flag (useful when the binding
    has policy semantics worth preserving); this removes the binding
    entirely, which is what the v1 DELETE endpoint exposes.
    """
    async with tenant_conn(tenant_id) as conn:
        # Tenant-scope guard mirrors ``enable_skill_for_coworker``.
        result = await conn.execute(
            """
            DELETE FROM coworker_skills cs
            USING coworkers c, skills s
            WHERE cs.coworker_id = $1::uuid
              AND cs.skill_id = $2::uuid
              AND c.id = cs.coworker_id AND c.tenant_id = $3::uuid
              AND s.id = cs.skill_id AND s.tenant_id = $3::uuid
            """,
            coworker_id,
            skill_id,
            tenant_id,
        )
    return result.endswith(" 1")


async def is_skill_bound_to_coworker(
    skill_id: str,
    coworker_id: str,
    *,
    tenant_id: str,
) -> bool:
    """Return ``True`` when a binding row exists for this pair.

    Looser than :func:`is_skill_enabled_for_coworker` — ignores both
    enabled flags. Backs the admin compatibility endpoints' auth
    check: the question "does this skill belong to this agent" was
    historically an ownership test (``skill.coworker_id == agent_id``)
    and stayed insensitive to enable/disable toggles. The strict
    "is it currently projection-eligible" semantics belong on the
    projection helper, not on permission gating — otherwise toggling
    a skill off locks admins out of toggling it back on.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM coworker_skills cs
            JOIN skills s ON s.id = cs.skill_id
            JOIN coworkers c ON c.id = cs.coworker_id
            WHERE cs.skill_id = $1::uuid
              AND cs.coworker_id = $2::uuid
              AND s.tenant_id = $3::uuid
              AND c.tenant_id = $3::uuid
            """,
            skill_id,
            coworker_id,
            tenant_id,
        )
    return row is not None


async def is_skill_enabled_for_coworker(
    skill_id: str,
    coworker_id: str,
    *,
    tenant_id: str,
) -> bool:
    """Return ``True`` only when both gates allow projection.

    Replaces the old "skill.coworker_id == agent_id" check that
    admin auth-guarded each skill endpoint with. The double-``enabled``
    AND matches ``list_skills_for_coworker(enabled_only=True)`` so
    REST auth, projection, and freeze agree on the same boolean.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM coworker_skills cs
            JOIN skills s ON s.id = cs.skill_id
            JOIN coworkers c ON c.id = cs.coworker_id
            WHERE cs.skill_id = $1::uuid
              AND cs.coworker_id = $2::uuid
              AND s.tenant_id = $3::uuid
              AND c.tenant_id = $3::uuid
              AND cs.enabled = TRUE
              AND s.enabled = TRUE
            """,
            skill_id,
            coworker_id,
            tenant_id,
        )
    return row is not None


async def list_coworkers_for_skill(
    skill_id: str,
    *,
    tenant_id: str,
) -> list[str]:
    """Return the coworker_ids bound to ``skill_id`` (any enabled state).

    The DELETE endpoint uses this to surface "in use by N coworkers"
    when refusing to drop a skill that still has bindings.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT cs.coworker_id
            FROM coworker_skills cs
            JOIN skills s ON s.id = cs.skill_id
            WHERE cs.skill_id = $1::uuid
              AND s.tenant_id = $2::uuid
            """,
            skill_id,
            tenant_id,
        )
    return [str(r["coworker_id"]) for r in rows]
