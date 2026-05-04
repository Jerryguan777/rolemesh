"""Coworker CRUD + user-agent assignments."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.types import ContainerConfig, Coworker, McpServerConfig, User
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
    tools: list[McpServerConfig] | None = None,
    container_config: ContainerConfig | None = None,
    max_concurrent: int = 2,
    agent_role: str = "agent",
    permissions: AgentPermissions | None = None,
) -> Coworker:
    """Create a new coworker."""
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
                tools, container_config, max_concurrent, agent_role, permissions)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10::jsonb)
            RETURNING *
            """,
            tenant_id,
            name,
            folder,
            agent_backend,
            system_prompt,
            json.dumps(
                [
                    {
                        "name": t.name,
                        "type": t.type,
                        "url": t.url,
                        "headers": t.headers,
                        "auth_mode": t.auth_mode,
                        "tool_reversibility": dict(t.tool_reversibility),
                    }
                    for t in tools
                ]
                if tools
                else []
            ),
            cc_json,
            max_concurrent,
            agent_role,
            json.dumps(effective_perms.to_dict()),
        )
    assert row is not None
    return _record_to_coworker(row)


def _record_to_coworker(row: asyncpg.Record) -> Coworker:
    tools_raw = row.get("tools")
    if isinstance(tools_raw, str):
        tools_raw = json.loads(tools_raw) if tools_raw else []
    elif not isinstance(tools_raw, list):
        tools_raw = []
    tools: list[McpServerConfig] = []
    for item in tools_raw:
        if isinstance(item, dict) and "name" in item:
            raw_headers = item.get("headers")
            auth_mode = item.get("auth_mode") or "user"
            if auth_mode not in ("user", "service", "both"):
                auth_mode = "user"
            raw_rev = item.get("tool_reversibility")
            tools.append(
                McpServerConfig(
                    name=item["name"],
                    type=item.get("type", "sse"),
                    url=item.get("url", ""),
                    headers=raw_headers if isinstance(raw_headers, dict) else {},
                    auth_mode=auth_mode,
                    tool_reversibility=(
                        dict(raw_rev) if isinstance(raw_rev, dict) else {}
                    ),
                )
            )
        # Skip legacy string entries silently

    # Parse agent_role and permissions (new auth fields)
    agent_role = row.get("agent_role") or "agent"
    perms_raw = row.get("permissions")
    if isinstance(perms_raw, dict):
        permissions = AgentPermissions.from_dict(perms_raw)
    elif isinstance(perms_raw, str) and perms_raw:
        permissions = AgentPermissions.from_dict(json.loads(perms_raw))
    else:
        permissions = AgentPermissions.for_role(agent_role)
    return Coworker(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        folder=row["folder"],
        agent_backend=row.get("agent_backend") or "claude",
        system_prompt=row.get("system_prompt"),
        tools=tools,
        container_config=_parse_container_config(row["container_config"]),
        max_concurrent=row["max_concurrent"],
        status=row["status"] or "active",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        agent_role=agent_role,
        permissions=permissions,
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


async def update_coworker(
    coworker_id: str,
    *,
    tenant_id: str,
    name: str | None = None,
    system_prompt: str | None = None,
    tools: list[McpServerConfig] | None = None,
    max_concurrent: int | None = None,
    status: str | None = None,
    agent_role: str | None = None,
    permissions: AgentPermissions | None = None,
) -> Coworker | None:
    """Update selected fields on a coworker, scoped to ``tenant_id``."""
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
    if tools is not None:
        fields.append(f"tools = ${param_idx}::jsonb")
        values.append(
            json.dumps(
                [
                    {
                        "name": t.name,
                        "type": t.type,
                        "url": t.url,
                        "headers": t.headers,
                        "auth_mode": t.auth_mode,
                        "tool_reversibility": dict(t.tool_reversibility),
                    }
                    for t in tools
                ]
            )
        )
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


