"""Coworker CRUD + user-agent assignments."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.types import ContainerConfig, Coworker, User
from rolemesh.db._pool import admin_conn, tenant_conn
from rolemesh.db.user import _record_to_user

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "assign_agent_to_user",
    "create_coworker",
    "delete_coworker",
    "get_agents_for_user",
    "get_all_coworkers",
    "get_coworker",
    "get_coworker_by_folder",
    "get_coworkers_for_tenant",
    "get_users_for_agent",
    "unassign_agent_from_user",
    "update_coworker",
]


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
    agent_role: str = "agent",
    permissions: AgentPermissions | None = None,
    model_id: str | None = None,
    created_by_user_id: str | None = None,
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
    """
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
    effective_perms = permissions or AgentPermissions.for_role(agent_role)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO coworkers (tenant_id, name, folder, agent_backend, system_prompt,
                container_config, max_concurrent, agent_role, permissions,
                model_id, created_by_user_id)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8, $9::jsonb,
                $10::uuid, $11::uuid)
            RETURNING *
            """,
            tenant_id,
            name,
            folder,
            agent_backend,
            system_prompt,
            cc_json,
            max_concurrent,
            agent_role,
            json.dumps(effective_perms.to_dict()),
            model_id,
            created_by_user_id,
        )
    assert row is not None
    return _record_to_coworker(row)


def _record_to_coworker(row: asyncpg.Record) -> Coworker:
    # Parse agent_role and permissions (new auth fields)
    agent_role = row.get("agent_role") or "agent"
    perms_raw = row.get("permissions")
    if isinstance(perms_raw, dict):
        permissions = AgentPermissions.from_dict(perms_raw)
    elif isinstance(perms_raw, str) and perms_raw:
        permissions = AgentPermissions.from_dict(json.loads(perms_raw))
    else:
        permissions = AgentPermissions.for_role(agent_role)
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
        agent_role=agent_role,
        permissions=permissions,
        model_id=str(model_id_val) if model_id_val else None,
        created_by_user_id=str(created_by_val) if created_by_val else None,
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


async def get_coworkers_for_tenant(tenant_id: str) -> list[Coworker]:
    """Get all coworkers for a tenant."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM coworkers WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_record_to_coworker(row) for row in rows]


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
    agent_role: str | None = None,
    permissions: AgentPermissions | None = None,
    model_id: str | None | Any = _MODEL_ID_UNSET,
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
    if agent_role is not None:
        fields.append(f"agent_role = ${param_idx}")
        values.append(agent_role)
        param_idx += 1
    if permissions is not None:
        fields.append(f"permissions = ${param_idx}::jsonb")
        values.append(json.dumps(permissions.to_dict()))
        param_idx += 1
    if model_id is not _MODEL_ID_UNSET:
        fields.append(f"model_id = ${param_idx}::uuid")
        values.append(model_id)
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




# ---------------------------------------------------------------------------
# User-Agent assignment CRUD
# ---------------------------------------------------------------------------


async def assign_agent_to_user(user_id: str, coworker_id: str, tenant_id: str) -> None:
    """Assign a coworker (agent) to a user."""
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO user_agent_assignments (user_id, coworker_id, tenant_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid)
            ON CONFLICT (user_id, coworker_id) DO NOTHING
            """,
            user_id,
            coworker_id,
            tenant_id,
        )


async def unassign_agent_from_user(
    user_id: str, coworker_id: str, *, tenant_id: str
) -> None:
    """Remove a coworker assignment from a user."""
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            "DELETE FROM user_agent_assignments "
            "WHERE user_id = $1::uuid AND coworker_id = $2::uuid "
            "AND tenant_id = $3::uuid",
            user_id,
            coworker_id,
            tenant_id,
        )


async def get_agents_for_user(user_id: str, *, tenant_id: str) -> list[Coworker]:
    """Get all coworkers assigned to a user, scoped to ``tenant_id``."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT c.* FROM coworkers c
            JOIN user_agent_assignments uaa ON c.id = uaa.coworker_id
            WHERE uaa.user_id = $1::uuid AND uaa.tenant_id = $2::uuid
            ORDER BY c.name
            """,
            user_id,
            tenant_id,
        )
    return [_record_to_coworker(row) for row in rows]


async def get_users_for_agent(coworker_id: str, *, tenant_id: str) -> list[User]:
    """Get all users assigned to a coworker, scoped to ``tenant_id``."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT u.* FROM users u
            JOIN user_agent_assignments uaa ON u.id = uaa.user_id
            WHERE uaa.coworker_id = $1::uuid AND uaa.tenant_id = $2::uuid
            ORDER BY u.name
            """,
            coworker_id,
            tenant_id,
        )
    return [_record_to_user(row) for row in rows]


