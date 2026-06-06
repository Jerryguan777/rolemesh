"""Tenant CRUD."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from rolemesh.core.types import Tenant
from rolemesh.db._pool import admin_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "create_tenant",
    "get_all_tenants",
    "get_tenant",
    "get_tenant_by_slug",
    "get_tenant_status",
    "set_tenant_status",
    "update_tenant",
    "update_tenant_message_cursor",
]


# ---------------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------------


async def create_tenant(
    name: str,
    slug: str | None = None,
    plan: str | None = None,
    max_concurrent_containers: int = 5,
) -> Tenant:
    """Create a new tenant and return it."""
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tenants (slug, name, plan, max_concurrent_containers)
            VALUES ($1, $2, $3, $4)
            RETURNING id, slug, name, plan, max_concurrent_containers,
                      last_message_cursor, created_at, status
            """,
            slug,
            name,
            plan,
            max_concurrent_containers,
        )
    assert row is not None
    return _record_to_tenant(row)


def _record_to_tenant(row: asyncpg.Record) -> Tenant:
    lmc = row["last_message_cursor"]
    return Tenant(
        id=str(row["id"]),
        name=row["name"],
        slug=row["slug"],
        plan=row["plan"],
        max_concurrent_containers=row["max_concurrent_containers"],
        last_message_cursor=lmc.isoformat() if lmc else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        status=row["status"],
    )


async def get_tenant(tenant_id: str) -> Tenant | None:
    """Get a tenant by ID."""
    async with admin_conn() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1::uuid", tenant_id)
    if row is None:
        return None
    return _record_to_tenant(row)


async def update_tenant(
    tenant_id: str,
    *,
    name: str | None = None,
    max_concurrent_containers: int | None = None,
) -> Tenant | None:
    """Update selected fields on a tenant."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if name is not None:
        fields.append(f"name = ${param_idx}")
        values.append(name)
        param_idx += 1
    if max_concurrent_containers is not None:
        fields.append(f"max_concurrent_containers = ${param_idx}")
        values.append(max_concurrent_containers)
        param_idx += 1

    if not fields:
        return await get_tenant(tenant_id)

    values.append(tenant_id)
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            f"UPDATE tenants SET {', '.join(fields)} WHERE id = ${param_idx}::uuid RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_tenant_by_slug(slug: str) -> Tenant | None:
    """Get a tenant by slug."""
    async with admin_conn() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE slug = $1", slug)
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_tenant_status(tenant_id: str) -> str | None:
    """Return a tenant's lifecycle status, or None if unknown.

    A focused single-column read for the authentication chokepoint, which
    runs on every authenticated request (see ``webui.dependencies``). A
    non-UUID id (the dev bootstrap path may carry the literal ``"default"``
    when no default tenant exists) returns None rather than raising, so the
    caller treats it as "not suspended" instead of 500-ing.
    """
    import uuid as _uuid

    try:
        _uuid.UUID(tenant_id)
    except (ValueError, AttributeError, TypeError):
        return None
    async with admin_conn() as conn:
        row = await conn.fetchrow("SELECT status FROM tenants WHERE id = $1::uuid", tenant_id)
    return row["status"] if row is not None else None


async def set_tenant_status(tenant_id: str, status: str) -> Tenant | None:
    """Set a tenant's lifecycle status ('active' | 'suspended').

    Platform-plane only (provision/suspend/resume). Returns the updated
    tenant, or None if no such tenant. The DB CHECK constraint rejects any
    value outside the allowed set, so a bad ``status`` fails loud rather
    than silently writing garbage.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "UPDATE tenants SET status = $1 WHERE id = $2::uuid RETURNING *",
            status,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_all_tenants() -> list[Tenant]:
    """Get all tenants."""
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT * FROM tenants ORDER BY created_at")
    return [_record_to_tenant(row) for row in rows]


async def update_tenant_message_cursor(tenant_id: str, cursor: str) -> None:
    """Update the last_message_cursor for a tenant."""
    ts = datetime.fromisoformat(cursor) if cursor else None
    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE tenants SET last_message_cursor = $1 WHERE id = $2::uuid",
            ts,
            tenant_id,
        )


