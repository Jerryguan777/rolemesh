"""User CRUD + OIDC token vault."""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

from rolemesh.core.types import User
from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "create_external_tenant_mapping",
    "create_user",
    "create_user_with_external_sub",
    "delete_user",
    "delete_user_oidc_tokens",
    "get_local_tenant_id",
    "get_user",
    "get_user_by_external_sub",
    "get_user_oidc_tokens",
    "get_users_for_tenant",
    "resolve_user_for_auth",
    "update_user",
    "update_user_access_token",
    "update_user_refresh_token",
    "upsert_user_oidc_tokens",
]


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


async def create_user(
    tenant_id: str,
    name: str,
    email: str | None = None,
    role: str = "member",
    channel_ids: dict[str, str] | None = None,
) -> User:
    """Create a new user."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, name, email, role, channel_ids)
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
            RETURNING id, tenant_id, name, email, role, channel_ids, created_at
            """,
            tenant_id,
            name,
            email,
            role,
            json.dumps(channel_ids or {}),
        )
    assert row is not None
    return _record_to_user(row)


def _record_to_user(row: asyncpg.Record) -> User:
    cids = row["channel_ids"]
    return User(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        email=row["email"],
        role=row["role"],
        channel_ids=cids if isinstance(cids, dict) else json.loads(cids) if cids else {},
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        external_sub=row.get("external_sub"),
    )


async def get_user_by_external_sub(external_sub: str) -> User | None:
    """Look up a user by their external OIDC subject identifier."""
    async with admin_conn() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE external_sub = $1", external_sub)
    if row is None:
        return None
    return _record_to_user(row)


async def create_user_with_external_sub(
    tenant_id: str,
    name: str,
    email: str | None,
    role: str,
    external_sub: str,
) -> User:
    """Create a user linked to an external OIDC subject."""
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, name, email, role, channel_ids, external_sub)
            VALUES ($1::uuid, $2, $3, $4, '{}'::jsonb, $5)
            RETURNING *
            """,
            tenant_id,
            name,
            email,
            role,
            external_sub,
        )
    assert row is not None
    return _record_to_user(row)


async def get_local_tenant_id(provider: str, external_tenant_id: str) -> str | None:
    """Look up the local tenant ID for an external IdP tenant."""
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT local_tenant_id FROM external_tenant_map WHERE provider = $1 AND external_tenant_id = $2",
            provider,
            external_tenant_id,
        )
    if row is None:
        return None
    return str(row["local_tenant_id"])


async def create_external_tenant_mapping(
    provider: str,
    external_tenant_id: str,
    local_tenant_id: str,
) -> None:
    """Create a mapping between an external IdP tenant and a local tenant."""
    async with admin_conn() as conn:
        await conn.execute(
            """
            INSERT INTO external_tenant_map (provider, external_tenant_id, local_tenant_id)
            VALUES ($1, $2, $3::uuid)
            ON CONFLICT (provider, external_tenant_id) DO NOTHING
            """,
            provider,
            external_tenant_id,
            local_tenant_id,
        )


# ---------------------------------------------------------------------------
# OIDC user token vault (server-side encrypted refresh/access tokens)
# ---------------------------------------------------------------------------


async def upsert_user_oidc_tokens(
    user_id: str,
    refresh_token_encrypted: bytes,
    access_token_encrypted: bytes | None,
    access_token_expires_at: datetime | None,
) -> None:
    """Insert or replace the encrypted token row for a user."""
    async with admin_conn() as conn:
        await conn.execute(
            """
            INSERT INTO oidc_user_tokens (
                user_id, refresh_token_encrypted, access_token_encrypted,
                access_token_expires_at, updated_at
            )
            VALUES ($1::uuid, $2, $3, $4, now())
            ON CONFLICT (user_id) DO UPDATE SET
                refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                access_token_encrypted = EXCLUDED.access_token_encrypted,
                access_token_expires_at = EXCLUDED.access_token_expires_at,
                updated_at = now()
            """,
            user_id,
            refresh_token_encrypted,
            access_token_encrypted,
            access_token_expires_at,
        )


async def get_user_oidc_tokens(
    user_id: str,
) -> tuple[bytes, bytes | None, datetime | None] | None:
    """Return (refresh_token_enc, access_token_enc, expires_at) or None."""
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT refresh_token_encrypted, access_token_encrypted, access_token_expires_at "
            "FROM oidc_user_tokens WHERE user_id = $1::uuid",
            user_id,
        )
    if row is None:
        return None
    return (
        row["refresh_token_encrypted"],
        row["access_token_encrypted"],
        row["access_token_expires_at"],
    )


async def update_user_access_token(
    user_id: str,
    access_token_encrypted: bytes,
    access_token_expires_at: datetime,
) -> None:
    """Update only the cached access_token (after refresh)."""
    async with admin_conn() as conn:
        await conn.execute(
            """
            UPDATE oidc_user_tokens
            SET access_token_encrypted = $1,
                access_token_expires_at = $2,
                updated_at = now()
            WHERE user_id = $3::uuid
            """,
            access_token_encrypted,
            access_token_expires_at,
            user_id,
        )


async def update_user_refresh_token(
    user_id: str,
    refresh_token_encrypted: bytes,
) -> None:
    """Update only the refresh_token (after IdP rotation)."""
    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE oidc_user_tokens SET refresh_token_encrypted = $1, updated_at = now() "
            "WHERE user_id = $2::uuid",
            refresh_token_encrypted,
            user_id,
        )


async def delete_user_oidc_tokens(user_id: str) -> None:
    """Remove a user's stored OIDC tokens (logout / refresh failure)."""
    async with admin_conn() as conn:
        await conn.execute(
            "DELETE FROM oidc_user_tokens WHERE user_id = $1::uuid",
            user_id,
        )


async def get_user(user_id: str, *, tenant_id: str) -> User | None:
    """Fetch a user by id, scoped to ``tenant_id``.

    Tenant scoping is on the query (not a post-fetch check) so a guess
    at another tenant's user_id returns None from the DB itself. The
    REST layer maps None to 404 — indistinguishable from "doesn't
    exist" so we don't leak UUID existence across tenants.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE id = $1::uuid AND tenant_id = $2::uuid",
            user_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_user(row)


async def resolve_user_for_auth(user_id: str) -> tuple[str, str] | None:
    """Look up ``(tenant_id, role)`` for a user by id alone.

    System-only escape hatch. The single legitimate caller is the
    AuthProvider's ``get_user_by_id`` (JWT resume path), which needs
    to recover the user's tenant_id before any tenant-scoped query
    can run. The user_id input must be from a signature-verified JWT
    claim — never from an unauthenticated request body.

    DO NOT use this from REST handlers. The return value carries
    authority — pair it with a tenant-scoped ``get_user`` once you
    have it.

    Returns None if the user_id does not exist.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id, role FROM users WHERE id = $1::uuid",
            user_id,
        )
    if row is None:
        return None
    return (str(row["tenant_id"]), str(row["role"]))


async def get_users_for_tenant(tenant_id: str) -> list[User]:
    """Get all users for a tenant."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM users WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_record_to_user(row) for row in rows]


async def update_user(
    user_id: str,
    *,
    tenant_id: str,
    name: str | None = None,
    email: str | None = None,
    role: str | None = None,
) -> User | None:
    """Update selected fields on a user, scoped to ``tenant_id``."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if name is not None:
        fields.append(f"name = ${param_idx}")
        values.append(name)
        param_idx += 1
    if email is not None:
        fields.append(f"email = ${param_idx}")
        values.append(email or None)  # "" → NULL in DB
        param_idx += 1
    if role is not None:
        fields.append(f"role = ${param_idx}")
        values.append(role)
        param_idx += 1

    if not fields:
        return await get_user(user_id, tenant_id=tenant_id)

    values.append(user_id)
    values.append(tenant_id)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"UPDATE users SET {', '.join(fields)} "
            f"WHERE id = ${param_idx}::uuid AND tenant_id = ${param_idx + 1}::uuid "
            f"RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_user(row)


async def delete_user(user_id: str, *, tenant_id: str) -> bool:
    """Delete a user by ID, scoped to ``tenant_id``."""
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM users WHERE id = $1::uuid AND tenant_id = $2::uuid",
            user_id,
            tenant_id,
        )
    return result == "DELETE 1"


