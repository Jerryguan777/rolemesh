"""``/api/v1/skills`` and ``/api/v1/coworkers/{id}/skills`` REST surface.

Design §3 Phase 3 / docs/19-skills-architecture.md. The catalog is
per-tenant (post-03b PR 1); coworker association lives in
``coworker_skills``. This module covers:

* Flat CRUD on ``skills`` + per-file PUT/GET/DELETE.
* Relation layer on the ``coworker_skills`` junction.

Every mutating handler publishes one ``web.coworker.skills_changed``
event per affected coworker so the orchestrator refreshes the
in-memory skills projection without a process restart (design §7
hot-load matrix). Catalog-only edits fan out to every bound coworker;
single-binding edits broadcast for the one coworker only.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.skills import (
    SKILL_MANIFEST_NAME,
    SkillValidationError,
    parse_inbound_skill_md,
    validate_skill_file_path,
    validate_skill_name,
)
from rolemesh.core.types import Skill as SkillDataclass
from rolemesh.core.types import SkillFile as SkillFileDataclass
from rolemesh.db import (
    create_skill,
    delete_skill,
    delete_skill_file,
    disable_skill_for_coworker,
    enable_skill_for_coworker,
    get_coworker,
    get_skill,
    list_coworkers_for_skill,
    list_skills_for_coworker,
    list_skills_for_tenant,
    set_skill_file,
    update_skill,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import (
    CoworkerSkillBinding,
    Skill,
    SkillCreate,
    SkillFile,
    SkillFileUpsert,
    SkillSummary,
    SkillUpdate,
)
from webui.v1 import coworker_events
from webui.v1.errors import ErrorResponseException, raise_error_response

skills_router = APIRouter(prefix="/skills", tags=["Skills"])
coworker_skills_router = APIRouter(
    prefix="/coworkers/{coworker_id}/skills",
    tags=["Coworkers"],
)


# ---------------------------------------------------------------------------
# Wire projections
# ---------------------------------------------------------------------------


def _skill_to_response(s: SkillDataclass) -> Skill:
    return Skill(
        id=s.id,
        tenant_id=s.tenant_id,
        name=s.name,
        enabled=s.enabled,
        frontmatter_common=s.frontmatter_common,
        frontmatter_backend=s.frontmatter_backend,
        files={
            p: SkillFile(
                path=f.path,
                content=f.content,
                mime_type=f.mime_type,
                updated_at=f.updated_at,
            )
            for p, f in s.files.items()
        },
        created_at=s.created_at,
        updated_at=s.updated_at,
        created_by_user_id=s.created_by_user_id,
    )


def _skill_file_to_response(f: SkillFileDataclass) -> SkillFile:
    return SkillFile(
        path=f.path,
        content=f.content,
        mime_type=f.mime_type,
        updated_at=f.updated_at,
    )


def _skill_to_summary(s: SkillDataclass, bound_count: int) -> SkillSummary:
    desc_raw = s.frontmatter_common.get("description", "")
    description = desc_raw if isinstance(desc_raw, str) else ""
    return SkillSummary(
        id=s.id,
        tenant_id=s.tenant_id,
        name=s.name,
        description=description,
        enabled=s.enabled,
        bound_coworker_count=bound_count,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_files(
    files_in: dict[str, str | SkillFileUpsert],
) -> dict[str, SkillFileDataclass]:
    """Convert wire shape (``str | SkillFileUpsert``) → dataclass.

    Mirrors the admin helper but uses the v1 ``SkillFileUpsert``
    schema. Validates each path before we hand the map to the DB
    layer — invalid paths fail with 422 (Unprocessable) rather than
    600 from the DB CHECK.
    """
    out: dict[str, SkillFileDataclass] = {}
    for path, value in files_in.items():
        try:
            validate_skill_file_path(path)
        except SkillValidationError as exc:
            raise ErrorResponseException(
                status_code=422,
                code="INVALID_PATH",
                message=str(exc),
                details={"path": path},
            ) from exc
        if isinstance(value, str):
            out[path] = SkillFileDataclass(path=path, content=value)
        else:
            out[path] = SkillFileDataclass(
                path=path,
                content=value.content,
                mime_type=value.mime_type,
            )
    return out


async def _get_skill_or_404(skill_id: str, *, tenant_id: str) -> SkillDataclass:
    try:
        skill = await get_skill(skill_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        skill = None
    if skill is None:
        raise_error_response(
            "NOT_FOUND",
            "Skill not found.",
            status_code=404,
            details={"skill_id": skill_id},
        )
    return skill


async def _ensure_coworker(coworker_id: str, *, tenant_id: str) -> None:
    try:
        cw = await get_coworker(coworker_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        cw = None
    if cw is None:
        raise_error_response(
            "NOT_FOUND",
            "Coworker not found.",
            status_code=404,
            details={"coworker_id": coworker_id},
        )


async def _broadcast_skills_changed(
    skill_id: str, *, tenant_id: str,
) -> None:
    """Publish ``skills_changed`` for every coworker currently bound
    to ``skill_id``. Catalog edits fan out so each cached projection
    in the orchestrator process can refresh.
    """
    coworker_ids = await list_coworkers_for_skill(
        skill_id, tenant_id=tenant_id,
    )
    for cw_id in coworker_ids:
        await coworker_events.publish_coworker_skills_changed(
            coworker_id=cw_id, tenant_id=tenant_id,
        )


# ---------------------------------------------------------------------------
# Flat /api/v1/skills
# ---------------------------------------------------------------------------


@skills_router.get("", response_model=list[SkillSummary])
async def list_skills_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SkillSummary]:
    skills = await list_skills_for_tenant(user.tenant_id)
    summaries: list[SkillSummary] = []
    for s in skills:
        bound = await list_coworkers_for_skill(s.id, tenant_id=user.tenant_id)
        summaries.append(_skill_to_summary(s, bound_count=len(bound)))
    return summaries


@skills_router.post("", response_model=Skill, status_code=201)
async def create_skill_endpoint(
    body: SkillCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Skill:
    try:
        validate_skill_name(body.name)
    except SkillValidationError as exc:
        raise ErrorResponseException(
            status_code=422,
            code="INVALID_NAME",
            message=str(exc),
            details={"name": body.name},
        ) from exc

    files = _normalize_files(body.files)
    if SKILL_MANIFEST_NAME not in files:
        raise ErrorResponseException(
            status_code=422,
            code="SKILL_MANIFEST_REQUIRED",
            message=f"'files' must include {SKILL_MANIFEST_NAME}.",
        )
    try:
        common, backend, body_text = parse_inbound_skill_md(
            files[SKILL_MANIFEST_NAME].content,
            frontmatter_common_override=body.frontmatter_common,
            frontmatter_backend_override=body.frontmatter_backend,
            expected_skill_name=body.name,
        )
    except SkillValidationError as exc:
        raise ErrorResponseException(
            status_code=400,
            code="INVALID_MANIFEST",
            message=str(exc),
        ) from exc
    # Drop frontmatter from the on-disk SKILL.md — it now lives in
    # the JSONB columns. Mirrors the admin handler.
    files[SKILL_MANIFEST_NAME] = SkillFileDataclass(
        path=SKILL_MANIFEST_NAME,
        content=body_text,
        mime_type=files[SKILL_MANIFEST_NAME].mime_type or "text/markdown",
    )
    try:
        skill = await create_skill(
            tenant_id=user.tenant_id,
            name=body.name,
            frontmatter_common=common,
            frontmatter_backend=backend,
            files=files,
            enabled=body.enabled,
            created_by_user_id=_uuid_or_none(user.user_id),
        )
    except asyncpg.UniqueViolationError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message="A skill with this name already exists in the tenant.",
            details={"name": body.name},
        ) from exc
    except asyncpg.CheckViolationError as exc:
        raise ErrorResponseException(
            status_code=400,
            code="INVALID_PAYLOAD",
            message=str(exc),
        ) from exc
    return _skill_to_response(skill)


def _uuid_or_none(user_id: str | None) -> str | None:
    """Coerce ``user_id`` to a UUID string or ``None``.

    Bootstrap admin uses ``"bootstrap"`` as the user id which is not
    a valid UUID; storing NULL beats letting asyncpg raise. Anything
    that already looks like a UUID is forwarded unchanged.
    """
    if not user_id:
        return None
    import uuid

    try:
        return str(uuid.UUID(user_id))
    except (ValueError, AttributeError, TypeError):
        return None


@skills_router.get("/{skill_id}", response_model=Skill)
async def get_skill_endpoint(
    skill_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Skill:
    skill = await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    return _skill_to_response(skill)


@skills_router.patch("/{skill_id}", response_model=Skill)
async def update_skill_endpoint(
    skill_id: str,
    body: SkillUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Skill:
    existing = await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    set_fields = body.model_fields_set

    # ``name`` is read-only on PATCH (see SkillUpdate docstring). Accept
    # it when it matches the existing value so a frontend can submit a
    # full snapshot without special-casing edit mode; reject when it
    # actually differs.
    if "name" in set_fields and body.name != existing.name:
        raise ErrorResponseException(
            status_code=400,
            code="INVALID_PAYLOAD",
            message=(
                f"skill name is immutable on PATCH "
                f"(got {body.name!r}, existing {existing.name!r})"
            ),
        )

    # File-set replacement path. When ``files`` is set we treat it as
    # the new full file map (same semantics as create) — the DB layer
    # atomically swaps the skill_files rows so SKILL.md and extras land
    # in one transaction.
    files_for_db: dict[str, SkillFileDataclass] | None = None
    common: dict[str, object] | None = None
    backend: dict[str, dict[str, object]] | None = None
    if "files" in set_fields and body.files is not None:
        files_for_db = _normalize_files(body.files)
        if SKILL_MANIFEST_NAME not in files_for_db:
            raise ErrorResponseException(
                status_code=422,
                code="SKILL_MANIFEST_REQUIRED",
                message=f"'files' must include {SKILL_MANIFEST_NAME}.",
            )
        # Re-parse SKILL.md so the frontmatter dicts stay in sync with
        # the new body. Mirror the create path's semantics: the inline
        # frontmatter in the new SKILL.md is canonical, and only
        # explicit body-level overrides win. Do NOT seed the override
        # from ``existing.frontmatter_common`` here — that would
        # clobber an edited inline description with the stale stored
        # value (test_patch_files_refreshes_frontmatter_description
        # pins this).
        common_override = (
            body.frontmatter_common
            if body.frontmatter_common is not None
            else None
        )
        backend_override = (
            body.frontmatter_backend
            if body.frontmatter_backend is not None
            else None
        )
        try:
            parsed_common, parsed_backend, body_text = parse_inbound_skill_md(
                files_for_db[SKILL_MANIFEST_NAME].content,
                frontmatter_common_override=common_override,
                frontmatter_backend_override=backend_override,
                expected_skill_name=existing.name,
            )
        except SkillValidationError as exc:
            raise ErrorResponseException(
                status_code=400,
                code="INVALID_MANIFEST",
                message=str(exc),
            ) from exc
        # Strip frontmatter from on-disk SKILL.md — it now lives in the
        # JSONB columns. Same shape as create_skill_endpoint.
        files_for_db[SKILL_MANIFEST_NAME] = SkillFileDataclass(
            path=SKILL_MANIFEST_NAME,
            content=body_text,
            mime_type=(
                files_for_db[SKILL_MANIFEST_NAME].mime_type or "text/markdown"
            ),
        )
        common, backend = parsed_common, parsed_backend
    elif "frontmatter_common" in set_fields or "frontmatter_backend" in set_fields:
        # Metadata-only frontmatter edit: keep existing SKILL.md body,
        # re-route through parse_inbound_skill_md so validation rules
        # (allowlists, name match, description bounds) still fire.
        current_md = existing.files.get(SKILL_MANIFEST_NAME)
        current_body = current_md.content if current_md else ""
        common_override = (
            body.frontmatter_common
            if body.frontmatter_common is not None
            else {}
        )
        backend_override = (
            body.frontmatter_backend
            if body.frontmatter_backend is not None
            else existing.frontmatter_backend
        )
        try:
            parsed_common, parsed_backend, _ = parse_inbound_skill_md(
                current_body,
                frontmatter_common_override={
                    **existing.frontmatter_common,
                    **common_override,
                },
                frontmatter_backend_override=backend_override,
                expected_skill_name=existing.name,
            )
        except SkillValidationError as exc:
            raise ErrorResponseException(
                status_code=400,
                code="INVALID_MANIFEST",
                message=str(exc),
            ) from exc
        common, backend = parsed_common, parsed_backend

    try:
        updated = await update_skill(
            skill_id,
            tenant_id=user.tenant_id,
            frontmatter_common=common,
            frontmatter_backend=backend,
            enabled=body.enabled,
            files=files_for_db,
        )
    except asyncpg.CheckViolationError as exc:
        raise ErrorResponseException(
            status_code=400,
            code="INVALID_PAYLOAD",
            message=str(exc),
        ) from exc
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Skill not found.",
            status_code=404,
            details={"skill_id": skill_id},
        )
    await _broadcast_skills_changed(skill_id, tenant_id=user.tenant_id)
    return _skill_to_response(updated)


@skills_router.delete("/{skill_id}", status_code=204)
async def delete_skill_endpoint(
    skill_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    coworker_ids = await list_coworkers_for_skill(
        skill_id, tenant_id=user.tenant_id,
    )
    if coworker_ids:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message=(
                f"Skill is bound to {len(coworker_ids)} coworker(s); "
                "unbind them before deleting."
            ),
            details={"coworker_ids": coworker_ids, "skill_id": skill_id},
        )
    removed = await delete_skill(skill_id, tenant_id=user.tenant_id)
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "Skill not found.",
            status_code=404,
            details={"skill_id": skill_id},
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# File endpoints
# ---------------------------------------------------------------------------


@skills_router.get("/{skill_id}/files", response_model=list[str])
async def list_skill_files_endpoint(
    skill_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[str]:
    skill = await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    return sorted(skill.files.keys())


@skills_router.get("/{skill_id}/files/{path:path}", response_model=SkillFile)
async def get_skill_file_endpoint(
    skill_id: str,
    path: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SkillFile:
    skill = await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    if path not in skill.files:
        raise_error_response(
            "NOT_FOUND",
            "File not found in skill.",
            status_code=404,
            details={"skill_id": skill_id, "path": path},
        )
    return _skill_file_to_response(skill.files[path])


@skills_router.put("/{skill_id}/files/{path:path}", response_model=SkillFile)
async def put_skill_file_endpoint(
    skill_id: str,
    path: str,
    body: SkillFileUpsert,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SkillFile:
    await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    try:
        validate_skill_file_path(path)
    except SkillValidationError as exc:
        raise ErrorResponseException(
            status_code=422,
            code="INVALID_PATH",
            message=str(exc),
            details={"path": path},
        ) from exc
    try:
        written = await set_skill_file(
            skill_id, path,
            tenant_id=user.tenant_id,
            content=body.content,
            mime_type=body.mime_type,
        )
    except asyncpg.CheckViolationError as exc:
        raise ErrorResponseException(
            status_code=422,
            code="INVALID_PATH",
            message=str(exc),
            details={"path": path},
        ) from exc
    if written is None:
        raise_error_response(
            "NOT_FOUND",
            "Skill not found.",
            status_code=404,
            details={"skill_id": skill_id},
        )
    await _broadcast_skills_changed(skill_id, tenant_id=user.tenant_id)
    return _skill_file_to_response(written)


@skills_router.delete("/{skill_id}/files/{path:path}", status_code=204)
async def delete_skill_file_endpoint(
    skill_id: str,
    path: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    if path == SKILL_MANIFEST_NAME:
        raise ErrorResponseException(
            status_code=409,
            code="SKILL_MANIFEST_PROTECTED",
            message=(
                f"{SKILL_MANIFEST_NAME} cannot be deleted; "
                "delete the whole skill or replace its contents."
            ),
            details={"skill_id": skill_id, "path": path},
        )
    try:
        validate_skill_file_path(path)
    except SkillValidationError as exc:
        raise ErrorResponseException(
            status_code=422,
            code="INVALID_PATH",
            message=str(exc),
            details={"path": path},
        ) from exc
    deleted = await delete_skill_file(
        skill_id, path, tenant_id=user.tenant_id,
    )
    if not deleted:
        raise_error_response(
            "NOT_FOUND",
            "File not found in skill.",
            status_code=404,
            details={"skill_id": skill_id, "path": path},
        )
    await _broadcast_skills_changed(skill_id, tenant_id=user.tenant_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Coworker <-> skill relation layer
# ---------------------------------------------------------------------------


@coworker_skills_router.get("", response_model=list[CoworkerSkillBinding])
async def list_coworker_skills_endpoint(
    coworker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[CoworkerSkillBinding]:
    """List every catalog skill bound to ``coworker_id`` (any state).

    The endpoint surfaces the binding rows so the UI can render
    "this coworker has skill X enabled / disabled" without a second
    round-trip to fetch the catalog flag. The catalog skill's own
    ``enabled`` flag remains a separate concern (queryable via
    ``GET /api/v1/skills/{id}``).
    """
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    skills = await list_skills_for_coworker(
        coworker_id, tenant_id=user.tenant_id,
    )
    # ``list_skills_for_coworker`` returns the catalog rows; we want
    # the binding shape. The catalog flag is on each skill; the
    # binding flag is implicit (only enabled bindings show up in the
    # filtered query — but the no-filter form returns every binding
    # regardless). For the wire shape we want to mirror the actual
    # ``coworker_skills.enabled`` value. Issue a small second query
    # to read the junction flags; the orchestrator caches the
    # projection separately so this isn't on the spawn path.
    from rolemesh.db._pool import tenant_conn

    async with tenant_conn(user.tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT cs.skill_id, cs.enabled
            FROM coworker_skills cs
            JOIN skills s ON s.id = cs.skill_id
            WHERE cs.coworker_id = $1::uuid
              AND s.tenant_id = $2::uuid
            """,
            coworker_id, user.tenant_id,
        )
    by_skill = {str(r["skill_id"]): bool(r["enabled"]) for r in rows}
    return [
        CoworkerSkillBinding(
            coworker_id=coworker_id,
            skill_id=s.id,
            enabled=by_skill.get(s.id, True),
        )
        for s in skills
    ]


@coworker_skills_router.post(
    "/{skill_id}", response_model=CoworkerSkillBinding, status_code=201,
)
async def enable_coworker_skill_endpoint(
    coworker_id: str,
    skill_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CoworkerSkillBinding:
    """Bind a catalog skill to this coworker (idempotent).

    Re-enabling an already-enabled binding is a no-op. Re-enabling
    a disabled binding flips the junction flag back to true. Both
    cases publish ``skills_changed``.
    """
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    await _get_skill_or_404(skill_id, tenant_id=user.tenant_id)
    ok = await enable_skill_for_coworker(
        skill_id=skill_id,
        coworker_id=coworker_id,
        tenant_id=user.tenant_id,
        enabled=True,
    )
    if not ok:
        raise_error_response(
            "NOT_FOUND",
            "Skill or coworker not in this tenant.",
            status_code=404,
            details={"skill_id": skill_id, "coworker_id": coworker_id},
        )
    await coworker_events.publish_coworker_skills_changed(
        coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    return CoworkerSkillBinding(
        coworker_id=coworker_id, skill_id=skill_id, enabled=True,
    )


@coworker_skills_router.delete("/{skill_id}", status_code=204)
async def disable_coworker_skill_endpoint(
    coworker_id: str,
    skill_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Remove the ``coworker_skills`` binding row.

    Distinct from "enable=false" — this deletes the binding outright,
    matching the v1 DELETE convention. The catalog skill survives.
    """
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    removed = await disable_skill_for_coworker(
        skill_id=skill_id,
        coworker_id=coworker_id,
        tenant_id=user.tenant_id,
    )
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "Binding not found.",
            status_code=404,
            details={"skill_id": skill_id, "coworker_id": coworker_id},
        )
    await coworker_events.publish_coworker_skills_changed(
        coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    return Response(status_code=204)
