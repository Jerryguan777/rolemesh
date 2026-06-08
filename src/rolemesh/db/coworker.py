"""Coworker CRUD."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.types import ContainerConfig, Coworker
from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "count_coworkers_for_tenant",
    "create_coworker",
    "delete_coworker",
    "get_all_coworkers",
    "get_coworker",
    "get_coworker_by_folder",
    "get_coworkers_for_tenant",
    "set_coworker_visibility",
    "update_coworker",
]

# The two-valued visibility domain (mirrors the DB CHECK constraint).
_VALID_VISIBILITY: frozenset[str] = frozenset({"private", "shared"})


# ---------------------------------------------------------------------------
# Coworker CRUD
# ---------------------------------------------------------------------------


def _parse_container_config(raw: dict[str, Any] | str | None) -> ContainerConfig | None:
    """Parse container_config JSONB into ContainerConfig."""
    if not raw:
        return None
    parsed = raw if isinstance(raw, dict) else json.loads(raw)
    if not isinstance(parsed, dict):
        return None
    from rolemesh.core.types import AdditionalMount

    mounts = [
        AdditionalMount(
            host_path=m.get("host_path", ""),
            container_path=m.get("container_path"),
            readonly=m.get("readonly", True),
        )
        for m in parsed.get("additional_mounts", [])
    ]
    return ContainerConfig(
        additional_mounts=mounts,
        timeout=parsed.get("timeout", 300_000),
    )


async def create_coworker(
    tenant_id: str,
    name: str,
    folder: str,
    agent_backend: str = "claude",
    system_prompt: str | None = None,
    container_config: ContainerConfig | None = None,
    max_concurrent: int = 2,
    permissions: AgentPermissions | None = None,
    model_id: str | None = None,
    created_by_user_id: str | None = None,
    visibility: str = "private",
    is_frontdesk: bool = False,
    routing_description: str | None = None,
) -> Coworker:
    """Create a new coworker row.

    MCP servers are no longer stored inline on the coworker (v1.1 §2.1
    moved them to the ``mcp_servers`` table + ``coworker_mcp_servers``
    junction). Callers that need to seed bindings at create time should
    follow up with :func:`rolemesh.db.replace_coworker_mcp_configs`
    (admin convenience) or the v1 relation layer.

    ``model_id`` and ``created_by_user_id`` are v1.1 additions (design
    §2.2). Both are NULLABLE on the DB so the existing admin call
    sites that omit them keep working — the v1 router populates them.

    ``visibility`` (feat/roles PR3) defaults to ``'private'`` so a newly
    created coworker is a personal draft until its creator shares it.
    Validated against the same ``{'private','shared'}`` domain the DB
    CHECK enforces, so a bad value fails fast rather than as an opaque
    CHECK violation.
    """
    if visibility not in _VALID_VISIBILITY:
        raise ValueError(f"invalid visibility {visibility!r}")
    cc_json: str | None = None
    if container_config:
        cc_json = json.dumps(
            {
                "additional_mounts": [
                    {"host_path": m.host_path, "container_path": m.container_path, "readonly": m.readonly}
                    for m in container_config.additional_mounts
                ],
                "timeout": container_config.timeout,
            }
        )
    effective_perms = permissions or AgentPermissions()
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO coworkers (tenant_id, name, folder, agent_backend, system_prompt,
                container_config, max_concurrent, permissions,
                model_id, created_by_user_id, visibility,
                is_frontdesk, routing_description)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8::jsonb,
                $9::uuid, $10::uuid, $11, $12, $13)
            RETURNING *
            """,
            tenant_id,
            name,
            folder,
            agent_backend,
            system_prompt,
            cc_json,
            max_concurrent,
            json.dumps(effective_perms.to_dict()),
            model_id,
            created_by_user_id,
            visibility,
            is_frontdesk,
            routing_description,
        )
    assert row is not None
    return _record_to_coworker(row)


def _record_to_coworker(row: asyncpg.Record) -> Coworker:
    # Parse permissions (flat capability bits)
    perms_raw = row.get("permissions")
    if isinstance(perms_raw, dict):
        permissions = AgentPermissions.from_dict(perms_raw)
    elif isinstance(perms_raw, str) and perms_raw:
        permissions = AgentPermissions.from_dict(json.loads(perms_raw))
    else:
        permissions = AgentPermissions()
    model_id_val = row.get("model_id") if hasattr(row, "get") else None
    created_by_val = (
        row.get("created_by_user_id") if hasattr(row, "get") else None
    )
    return Coworker(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        folder=row["folder"],
        agent_backend=row.get("agent_backend") or "claude",
        system_prompt=row.get("system_prompt"),
        container_config=_parse_container_config(row["container_config"]),
        max_concurrent=row["max_concurrent"],
        status=row["status"] or "active",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        permissions=permissions,
        model_id=str(model_id_val) if model_id_val else None,
        created_by_user_id=str(created_by_val) if created_by_val else None,
        visibility=(
            row.get("visibility") if hasattr(row, "get") else None
        ) or "private",
        is_frontdesk=bool(row.get("is_frontdesk")),
        routing_description=row.get("routing_description"),
    )


async def get_coworker(coworker_id: str, *, tenant_id: str) -> Coworker | None:
    """Fetch a coworker by id, scoped to ``tenant_id``.

    See ``get_user`` for the tenant-filter rationale.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM coworkers WHERE id = $1::uuid AND tenant_id = $2::uuid",
            coworker_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_coworker(row)


async def get_coworker_by_folder(tenant_id: str, folder: str) -> Coworker | None:
    """Get a coworker by tenant and folder."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM coworkers WHERE tenant_id = $1::uuid AND folder = $2",
            tenant_id,
            folder,
        )
    if row is None:
        return None
    return _record_to_coworker(row)


def _coworker_visibility_sql(
    tenant_id: str,
    *,
    requesting_user_id: str | None,
    include_all: bool,
) -> tuple[str, list[object]]:
    """WHERE clause + params shared by the visibility-scoped list/count.

    ``created_by_user_id = $2`` is three-valued-logic safe: a row with
    ``created_by_user_id IS NULL`` yields ``NULL`` (not TRUE), so an
    un-attributed private row never leaks to a member. Managers pass
    ``include_all=True`` so no predicate is added.
    """
    where = "tenant_id = $1::uuid"
    params: list[object] = [tenant_id]
    if not include_all:
        params.append(requesting_user_id)
        where += " AND (visibility = 'shared' OR created_by_user_id = $2::uuid)"
    return where, params


async def get_coworkers_for_tenant(
    tenant_id: str,
    *,
    requesting_user_id: str | None = None,
    include_all: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[Coworker]:
    """Get coworkers for a tenant, optionally visibility-scoped and paginated.

    ``include_all`` (the DEFAULT) preserves the historical unfiltered
    behavior: every internal caller (orchestrator load paths, OIDC
    auto-assign) needs ALL rows and must NOT be visibility-scoped. Only
    the v1 list endpoint opts in to filtering by passing
    ``include_all=False`` together with ``requesting_user_id`` (the
    predicate is the SQL mirror of
    :func:`webui.dependencies.user_can_see_resource`'s SEE rule).
    """
    where, params = _coworker_visibility_sql(
        tenant_id, requesting_user_id=requesting_user_id, include_all=include_all,
    )
    sql = f"SELECT * FROM coworkers WHERE {where} ORDER BY name"
    if limit is not None:
        params.extend((limit, offset))
        sql += f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_coworker(row) for row in rows]


async def count_coworkers_for_tenant(
    tenant_id: str,
    *,
    requesting_user_id: str | None = None,
    include_all: bool = True,
) -> int:
    """Count coworkers visible to the caller (same predicate as the list)."""
    where, params = _coworker_visibility_sql(
        tenant_id, requesting_user_id=requesting_user_id, include_all=include_all,
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"SELECT COUNT(*) AS n FROM coworkers WHERE {where}", *params,
        )
    return int(row["n"]) if row else 0


async def set_coworker_visibility(
    coworker_id: str, *, visibility: str, tenant_id: str
) -> Coworker | None:
    """Flip a coworker's ``visibility`` (share / unshare).

    Validates the value against the same domain the DB CHECK enforces so
    a typo surfaces as ``ValueError`` rather than an opaque CHECK
    violation. Returns the updated row, or ``None`` when no coworker with
    that id exists in ``tenant_id`` (the handler maps that to 404).
    """
    if visibility not in _VALID_VISIBILITY:
        raise ValueError(f"invalid visibility {visibility!r}")
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE coworkers SET visibility = $1 "
            "WHERE id = $2::uuid AND tenant_id = $3::uuid RETURNING *",
            visibility,
            coworker_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_coworker(row)


async def get_all_coworkers() -> list[Coworker]:
    """Get all coworkers."""
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT * FROM coworkers ORDER BY tenant_id, name")
    return [_record_to_coworker(row) for row in rows]


_MODEL_ID_UNSET: Any = object()


async def update_coworker(
    coworker_id: str,
    *,
    tenant_id: str,
    name: str | None = None,
    system_prompt: str | None = None,
    max_concurrent: int | None = None,
    status: str | None = None,
    permissions: AgentPermissions | None = None,
    model_id: str | None | Any = _MODEL_ID_UNSET,
    is_frontdesk: bool | None = None,
    routing_description: str | None = None,
) -> Coworker | None:
    """Update selected fields on a coworker, scoped to ``tenant_id``.

    MCP server bindings have their own write path
    (:func:`rolemesh.db.replace_coworker_mcp_configs` /
    :func:`rolemesh.db.bind_coworker_mcp_server`); they are no longer
    accepted here.

    ``model_id`` uses a sentinel rather than ``None`` for "unchanged"
    because ``None`` is a legitimate clearing value — the v1 API
    rejects clearing today but the helper stays explicit so a future
    caller doesn't accidentally null the column by passing ``None``
    to mean "no change".
    """
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if name is not None:
        fields.append(f"name = ${param_idx}")
        values.append(name)
        param_idx += 1
    if system_prompt is not None:
        fields.append(f"system_prompt = ${param_idx}")
        values.append(system_prompt)
        param_idx += 1
    if max_concurrent is not None:
        fields.append(f"max_concurrent = ${param_idx}")
        values.append(max_concurrent)
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1
    if permissions is not None:
        fields.append(f"permissions = ${param_idx}::jsonb")
        values.append(json.dumps(permissions.to_dict()))
        param_idx += 1
    if model_id is not _MODEL_ID_UNSET:
        fields.append(f"model_id = ${param_idx}::uuid")
        values.append(model_id)
        param_idx += 1
    if is_frontdesk is not None:
        fields.append(f"is_frontdesk = ${param_idx}")
        values.append(is_frontdesk)
        param_idx += 1
    if routing_description is not None:
        fields.append(f"routing_description = ${param_idx}")
        values.append(routing_description)
        param_idx += 1

    if not fields:
        return await get_coworker(coworker_id, tenant_id=tenant_id)

    values.append(coworker_id)
    values.append(tenant_id)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"UPDATE coworkers SET {', '.join(fields)} "
            f"WHERE id = ${param_idx}::uuid AND tenant_id = ${param_idx + 1}::uuid "
            f"RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_coworker(row)


async def delete_coworker(coworker_id: str, *, tenant_id: str) -> bool:
    """Delete a coworker by ID, scoped to ``tenant_id``. CASCADE handles
    dependent tables."""
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM coworkers WHERE id = $1::uuid AND tenant_id = $2::uuid",
            coworker_id,
            tenant_id,
        )
    return result == "DELETE 1"


