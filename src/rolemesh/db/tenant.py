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
            RETURNING id, slug, name, plan, max_concurrent_containers, last_message_cursor, created_at
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
    # ``approval_default_mode`` may be missing on rows read via older
    # ``SELECT id, slug, name, plan, max_concurrent_containers,
    # last_message_cursor, created_at`` projections; default it here
    # so the dataclass stays stable regardless of the projection.
    try:
        default_mode = row["approval_default_mode"] or "auto_execute"
    except (KeyError, IndexError):
        default_mode = "auto_execute"
    return Tenant(
        id=str(row["id"]),
        name=row["name"],
        slug=row["slug"],
        plan=row["plan"],
        max_concurrent_containers=row["max_concurrent_containers"],
        last_message_cursor=lmc.isoformat() if lmc else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        approval_default_mode=default_mode,
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
    approval_default_mode: str | None = None,
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
    if approval_default_mode is not None:
        if approval_default_mode not in (
            "auto_execute", "require_approval", "deny",
        ):
            raise ValueError(
                f"invalid approval_default_mode: {approval_default_mode!r}"
            )
        fields.append(f"approval_default_mode = ${param_idx}")
        values.append(approval_default_mode)
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


