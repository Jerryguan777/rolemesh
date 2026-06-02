"""``mcp_servers`` CRUD helpers.

Tenant-scoped table — every read / write goes through
``tenant_conn`` with an explicit ``WHERE tenant_id`` predicate
(INV-1 belt-and-braces). The ``tool_reversibility`` column carries
the per-tool override map; the egress safety pipeline (00a INV-2)
filters unknown keys on the IPC side, so it is safe to pass arbitrary
shapes through untransformed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rolemesh.db._pool import tenant_conn

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


__all__ = [
    "CoworkerMCPBinding",
    "MCPServerRow",
    "bind_coworker_mcp_server",
    "create_mcp_server",
    "delete_mcp_server",
    "get_mcp_server",
    "get_mcp_server_references",
    "list_coworker_mcp_bindings",
    "list_mcp_servers",
    "set_coworker_mcp_enabled_tools",
    "unbind_coworker_mcp_server",
    "update_mcp_server",
]


_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class MCPServerRow:
    """Read projection of one ``mcp_servers`` row."""

    id: str
    tenant_id: str
    name: str
    type: str
    url: str
    auth_mode: str
    extra_headers: dict[str, Any] = field(default_factory=dict)
    tool_reversibility: dict[str, Any] = field(default_factory=dict)
    description: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _parse_jsonb(value: Any) -> dict[str, Any]:
    """asyncpg may surface JSONB as either str or dict.

    Without a registered codec the connection returns the raw text;
    inside ``admin_conn`` we sometimes have a codec registered.
    Normalise to dict so callers don't have to think about it.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def _row_to_dataclass(row: asyncpg.Record) -> MCPServerRow:
    return MCPServerRow(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        type=row["type"],
        url=row["url"],
        auth_mode=row["auth_mode"],
        extra_headers=_parse_jsonb(row["extra_headers"]),
        tool_reversibility=_parse_jsonb(row["tool_reversibility"]),
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_SELECT_COLUMNS = (
    "id, tenant_id, name, type, url, auth_mode, "
    "extra_headers, tool_reversibility, description, "
    "created_at, updated_at"
)


async def list_mcp_servers(tenant_id: str) -> list[MCPServerRow]:
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            f"SELECT {_SELECT_COLUMNS} FROM mcp_servers "
            "WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_row_to_dataclass(r) for r in rows]


async def get_mcp_server(
    mcp_id: str, *, tenant_id: str
) -> MCPServerRow | None:
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"SELECT {_SELECT_COLUMNS} FROM mcp_servers "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            mcp_id, tenant_id,
        )
    if row is None:
        return None
    return _row_to_dataclass(row)


async def create_mcp_server(
    *,
    tenant_id: str,
    name: str,
    type: str,
    url: str,
    auth_mode: str,
    extra_headers: dict[str, Any] | None = None,
    tool_reversibility: dict[str, Any] | None = None,
    description: str | None = None,
) -> MCPServerRow:
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"INSERT INTO mcp_servers ("
            "tenant_id, name, type, url, auth_mode, "
            "extra_headers, tool_reversibility, description) "
            "VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8) "
            f"RETURNING {_SELECT_COLUMNS}",
            tenant_id, name, type, url, auth_mode,
            json.dumps(extra_headers or {}),
            json.dumps(tool_reversibility or {}),
            description,
        )
    assert row is not None
    return _row_to_dataclass(row)


async def update_mcp_server(
    mcp_id: str,
    *,
    tenant_id: str,
    name: Any = _UNSET,
    type: Any = _UNSET,
    url: Any = _UNSET,
    auth_mode: Any = _UNSET,
    extra_headers: Any = _UNSET,
    tool_reversibility: Any = _UNSET,
    description: Any = _UNSET,
) -> MCPServerRow | None:
    """Partial update keyed by sentinels so callers can clear-to-null.

    Absence (``_UNSET``) means "leave the column alone"; explicit
    ``None`` clears it where the column is nullable. The wire layer
    is expected to translate Pydantic optionality into ``_UNSET`` for
    unset PATCH fields.
    """
    sets: list[str] = []
    params: list[Any] = []
    for col, value in (
        ("name", name),
        ("type", type),
        ("url", url),
        ("auth_mode", auth_mode),
        ("description", description),
    ):
        if value is _UNSET:
            continue
        params.append(value)
        sets.append(f"{col} = ${len(params)}")
    for col, value in (
        ("extra_headers", extra_headers),
        ("tool_reversibility", tool_reversibility),
    ):
        if value is _UNSET:
            continue
        params.append(json.dumps(value or {}))
        sets.append(f"{col} = ${len(params)}::jsonb")
    if not sets:
        return await get_mcp_server(mcp_id, tenant_id=tenant_id)
    sets.append("updated_at = NOW()")
    params.append(mcp_id)
    params.append(tenant_id)
    sql = (
        f"UPDATE mcp_servers SET {', '.join(sets)} "
        f"WHERE id = ${len(params) - 1}::uuid AND tenant_id = ${len(params)}::uuid "
        f"RETURNING {_SELECT_COLUMNS}"
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(sql, *params)
    if row is None:
        return None
    return _row_to_dataclass(row)


async def delete_mcp_server(mcp_id: str, *, tenant_id: str) -> bool:
    async with tenant_conn(tenant_id) as conn:
        status = await conn.execute(
            "DELETE FROM mcp_servers "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            mcp_id, tenant_id,
        )
    return status.endswith(" 1")


@dataclass(frozen=True, slots=True)
class CoworkerMCPBinding:
    """One row from ``coworker_mcp_servers``.

    ``enabled_tools`` is the tri-state column documented on the
    table: ``None`` means all tools enabled (the common case),
    ``[]`` means all disabled, and a non-empty list is a whitelist.
    Routes and tests rely on this distinction surviving the DB
    roundtrip — do not collapse ``None`` and ``[]`` here.
    """

    coworker_id: str
    mcp_server_id: str
    enabled_tools: list[str] | None


async def list_coworker_mcp_bindings(
    coworker_id: str, *, tenant_id: str,
) -> list[CoworkerMCPBinding]:
    """Return every MCP binding for ``coworker_id``.

    The join carries tenant_id explicitly so a malformed
    ``coworker_id`` cannot reach a foreign tenant's row even if
    the junction RLS policy were ever weakened.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT cms.coworker_id, cms.mcp_server_id, cms.enabled_tools "
            "FROM coworker_mcp_servers cms "
            "JOIN coworkers c ON c.id = cms.coworker_id "
            "WHERE cms.coworker_id = $1::uuid "
            "  AND c.tenant_id = $2::uuid",
            coworker_id, tenant_id,
        )
    out: list[CoworkerMCPBinding] = []
    for r in rows:
        et = r["enabled_tools"]
        # asyncpg returns TEXT[] as list[str] | None — keep the
        # tri-state semantics intact (do not coerce None to []).
        if et is not None and not isinstance(et, list):
            et = list(et)
        out.append(
            CoworkerMCPBinding(
                coworker_id=str(r["coworker_id"]),
                mcp_server_id=str(r["mcp_server_id"]),
                enabled_tools=et,
            )
        )
    return out


async def bind_coworker_mcp_server(
    *,
    coworker_id: str,
    mcp_server_id: str,
    enabled_tools: list[str] | None,
    tenant_id: str,
) -> CoworkerMCPBinding:
    """Create the (coworker, mcp_server) row with the given tri-state value.

    Caller is responsible for verifying both parent rows belong to
    ``tenant_id`` before calling; the junction's RLS policy
    transitively enforces it via the parent coworker but the
    route layer wants an explicit 404 path on missing parents.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO coworker_mcp_servers "
            "    (coworker_id, mcp_server_id, enabled_tools) "
            "VALUES ($1::uuid, $2::uuid, $3) "
            "RETURNING coworker_id, mcp_server_id, enabled_tools",
            coworker_id, mcp_server_id, enabled_tools,
        )
    assert row is not None
    return CoworkerMCPBinding(
        coworker_id=str(row["coworker_id"]),
        mcp_server_id=str(row["mcp_server_id"]),
        enabled_tools=row["enabled_tools"],
    )


async def unbind_coworker_mcp_server(
    *,
    coworker_id: str,
    mcp_server_id: str,
    tenant_id: str,
) -> bool:
    """Delete one binding. Returns ``True`` iff a row was removed."""
    async with tenant_conn(tenant_id) as conn:
        status = await conn.execute(
            "DELETE FROM coworker_mcp_servers cms "
            "USING coworkers c "
            "WHERE cms.coworker_id = $1::uuid "
            "  AND cms.mcp_server_id = $2::uuid "
            "  AND c.id = cms.coworker_id "
            "  AND c.tenant_id = $3::uuid",
            coworker_id, mcp_server_id, tenant_id,
        )
    return status.endswith(" 1")


async def set_coworker_mcp_enabled_tools(
    *,
    coworker_id: str,
    mcp_server_id: str,
    enabled_tools: list[str] | None,
    tenant_id: str,
) -> CoworkerMCPBinding | None:
    """Update the tri-state column on an existing binding.

    Returns ``None`` if the binding does not exist (route layer
    surfaces as 404). Distinguishes ``[]`` from ``None`` so the
    "ban all tools" intent reaches the DB intact.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "UPDATE coworker_mcp_servers cms "
            "SET enabled_tools = $3 "
            "FROM coworkers c "
            "WHERE cms.coworker_id = $1::uuid "
            "  AND cms.mcp_server_id = $2::uuid "
            "  AND c.id = cms.coworker_id "
            "  AND c.tenant_id = $4::uuid "
            "RETURNING cms.coworker_id, cms.mcp_server_id, cms.enabled_tools",
            coworker_id, mcp_server_id, enabled_tools, tenant_id,
        )
    if row is None:
        return None
    return CoworkerMCPBinding(
        coworker_id=str(row["coworker_id"]),
        mcp_server_id=str(row["mcp_server_id"]),
        enabled_tools=row["enabled_tools"],
    )


async def get_mcp_server_references(
    mcp_id: str, *, tenant_id: str
) -> list[str]:
    """Return the coworker IDs binding this MCP server.

    Used by DELETE to return a structured 409 with the offenders.
    Cross-checks both the explicit ``tenant_id`` and the join through
    ``coworkers.tenant_id`` so a bug in the relation table cannot
    smuggle cross-tenant references.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT c.id "
            "FROM coworker_mcp_servers cms "
            "JOIN coworkers c ON c.id = cms.coworker_id "
            "WHERE cms.mcp_server_id = $1::uuid AND c.tenant_id = $2::uuid",
            mcp_id, tenant_id,
        )
    return [str(r["id"]) for r in rows]
