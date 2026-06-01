"""``models`` + ``tenant_model_credentials`` helpers.

The ``models`` table is tenant-agnostic (no RLS) — every tenant
shares the platform catalog. ``tenant_model_credentials`` is
tenant-scoped; reads / writes go through ``tenant_conn`` so the
belt-and-braces RLS + ``WHERE tenant_id`` pattern (INV-1) applies.

v1.1 §8.1: credentials now store Fernet-encrypted JSON in the BYTEA
``credential_data`` column. These helpers move bytes opaquely — they
do not parse, log, or otherwise observe the plaintext. Encryption /
decryption is the route layer's responsibility via
:mod:`rolemesh.auth.credential_vault`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg


__all__ = [
    "CredentialRow",
    "ModelRow",
    "count_coworkers_using_model",
    "create_model",
    "delete_tenant_credential",
    "get_coworker_ids_for_tenant_provider",
    "get_model_by_id",
    "list_models",
    "list_tenant_credentials",
    "soft_delete_model",
    "tenant_has_credential_for_provider",
    "update_model",
    "upsert_tenant_credential",
]


@dataclass(frozen=True, slots=True)
class ModelRow:
    """Projection of a ``models`` row for the v1 API.

    Carries the fields the SPA renders on the read-only catalog page
    plus the metadata the create-coworker validation chain needs.
    """

    id: str
    provider: str
    model_id: str
    model_family: str
    display_name: str
    is_active: bool
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CredentialRow:
    """Tenant credential metadata WITHOUT the encrypted payload.

    Deliberately omits ``credential_data`` so a caller cannot
    accidentally leak the ciphertext through a route that returns
    this dataclass — same defence-in-depth posture the wire schema
    uses (the Pydantic ``CredentialResponse`` does not declare a
    ``credential_data`` field either).
    """

    provider: str
    created_at: datetime
    updated_at: datetime


def _record_to_model(row: "asyncpg.Record") -> ModelRow:
    return ModelRow(
        id=str(row["id"]),
        provider=row["provider"],
        model_id=row["model_id"],
        model_family=row["model_family"],
        display_name=row["display_name"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"] if "created_at" in row.keys() else None,
    )


async def get_model_by_id(model_id: str) -> ModelRow | None:
    """Return the platform model row or ``None``.

    Uses the admin pool because ``models`` has no RLS (every tenant
    sees every row); going through ``tenant_conn`` would still work
    but adds a pointless GUC transaction.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, provider, model_id, model_family, display_name, "
            "is_active, created_at FROM models WHERE id = $1::uuid",
            model_id,
        )
    if row is None:
        return None
    return _record_to_model(row)


async def list_models(
    *,
    provider: str | None = None,
    family: str | None = None,
    only_active: bool = True,
) -> list[ModelRow]:
    """Return all platform models, optionally filtered by provider/family.

    Filters are AND-combined. ``only_active`` defaults to True so the
    SPA picker doesn't show deprecated rows; the underlying call
    sites that need the inactive ones (admin tooling) pass False.
    Rows are sorted ``(provider, display_name)`` for a stable
    pickability order.
    """
    where_clauses: list[str] = []
    params: list[object] = []
    if provider is not None:
        params.append(provider)
        where_clauses.append(f"provider = ${len(params)}")
    if family is not None:
        params.append(family)
        where_clauses.append(f"model_family = ${len(params)}")
    if only_active:
        where_clauses.append("is_active = TRUE")
    sql = (
        "SELECT id, provider, model_id, model_family, display_name, "
        "is_active, created_at FROM models"
    )
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY provider, display_name"
    async with admin_conn() as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_model(r) for r in rows]


async def create_model(
    *,
    provider: str,
    model_id: str,
    model_family: str,
    display_name: str,
    is_active: bool = True,
) -> ModelRow:
    """Insert a new ``models`` row and return the dataclass.

    Admin-only write surface (PR24). Raises :class:`asyncpg.UniqueViolationError`
    when the ``(provider, model_id)`` pair already exists; the route
    layer translates that to a 409. The schema has no other write-time
    constraints worth catching here — the Pydantic field validators on
    the API layer enforce length / enum.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "INSERT INTO models "
            "    (provider, model_id, model_family, display_name, is_active) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, provider, model_id, model_family, "
            "          display_name, is_active, created_at",
            provider, model_id, model_family, display_name, is_active,
        )
    assert row is not None
    return _record_to_model(row)


async def update_model(
    model_id: str,
    *,
    display_name: str | None = None,
    is_active: bool | None = None,
) -> ModelRow | None:
    """Update mutable fields. Returns the updated row or None if absent.

    Only ``display_name`` and ``is_active`` are mutable: provider /
    model_id / model_family form the identity tuple — renaming any of
    them would silently change which underlying provider model a
    bound coworker resolves to (could swap a Claude config for an
    OpenAI one mid-conversation). Identity changes belong in a fresh
    row + migration of the bindings, not an UPDATE.
    """
    fields: list[str] = []
    values: list[object] = []
    if display_name is not None:
        values.append(display_name)
        fields.append(f"display_name = ${len(values)}")
    if is_active is not None:
        values.append(is_active)
        fields.append(f"is_active = ${len(values)}")
    if not fields:
        # No-op update — return the existing row so the caller still
        # gets a 200. Matches the SkillUpdate semantics where an
        # empty body is legal.
        return await get_model_by_id(model_id)
    values.append(model_id)
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            f"UPDATE models SET {', '.join(fields)} "
            f"WHERE id = ${len(values)}::uuid "
            "RETURNING id, provider, model_id, model_family, "
            "          display_name, is_active, created_at",
            *values,
        )
    if row is None:
        return None
    return _record_to_model(row)


async def count_coworkers_using_model(model_id: str) -> int:
    """Count coworkers (across all tenants) bound to ``model_id``.

    Used by the soft-delete path to decide whether retiring the model
    would orphan bindings. Goes through ``admin_conn`` because we
    intentionally want the cross-tenant count: an operator deleting a
    platform model needs to see total impact, not just their own
    tenant's. Frontend never sees this count — it's a 409 gating
    signal only.
    """
    # inv-1-ok: deliberate cross-tenant aggregate — returns only a COUNT
    # (never row data) so an operator can see a platform model's total usage
    # before retiring it (see docstring); never exposed to a tenant.
    async with admin_conn() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM coworkers WHERE model_id = $1::uuid",
            model_id,
        )
    return int(n or 0)


async def soft_delete_model(model_id: str) -> bool:
    """Flip ``is_active`` to False. Returns True iff a row was updated.

    Soft delete instead of DROP because:
    * Existing coworkers bound to the model continue to render their
      model name in lists; a true DELETE + FK cascade would silently
      blank them out.
    * The catalog historically grows append-only (every model the
      platform has ever offered is in the table); operators need a
      reversible deprecation, not a destructive remove.
    """
    async with admin_conn() as conn:
        status = await conn.execute(
            "UPDATE models SET is_active = FALSE "
            "WHERE id = $1::uuid AND is_active = TRUE",
            model_id,
        )
    return status.endswith(" 1")


async def tenant_has_credential_for_provider(
    tenant_id: str, provider: str
) -> bool:
    """Return ``True`` iff the tenant has a credential row for ``provider``.

    Belt-and-braces: even though RLS is enabled on
    ``tenant_model_credentials``, the explicit ``WHERE tenant_id``
    predicate satisfies INV-1.
    """
    async with tenant_conn(tenant_id) as conn:
        return bool(
            await conn.fetchval(
                "SELECT 1 FROM tenant_model_credentials "
                "WHERE tenant_id = $1::uuid AND provider = $2 "
                "LIMIT 1",
                tenant_id,
                provider,
            )
        )


async def get_credential_ciphertext(
    tenant_id: str, provider: str
) -> bytes | None:
    """Return the Fernet-encrypted credential blob, or ``None`` if absent.

    Deliberately exposes ciphertext — the rest of this module's
    credential helpers strip ``credential_data`` from their projections
    (see :class:`CredentialRow`). This single function is the carve-out:
    :class:`rolemesh.egress.credentials.CredentialResolver` calls it,
    immediately Fernet-decrypts the blob into an in-process dict, and
    never reads the bytes again. Adding a second caller requires
    auditing that the bytes do not escape the process.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchval(
            "SELECT credential_data FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid AND provider = $2 "
            "LIMIT 1",
            tenant_id,
            provider,
        )
    if row is None:
        return None
    return bytes(row)


async def list_tenant_credentials(tenant_id: str) -> list[CredentialRow]:
    """Return all credential rows for the tenant (no ciphertext)."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT provider, created_at, updated_at "
            "FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid "
            "ORDER BY provider",
            tenant_id,
        )
    return [
        CredentialRow(
            provider=r["provider"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


async def upsert_tenant_credential(
    *, tenant_id: str, provider: str, credential_data: bytes
) -> CredentialRow:
    """Insert or update a tenant credential row; return metadata.

    ``credential_data`` is the Fernet ciphertext (the caller already
    ran ``CredentialVault.encrypt_json``). Uses
    ``ON CONFLICT ... DO UPDATE`` keyed on
    ``UNIQUE (tenant_id, provider)`` so the call is idempotent. The
    RETURNING clause surfaces ``created_at`` and ``updated_at`` so
    the response can show "first seen" vs "last touched".
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO tenant_model_credentials "
            "    (tenant_id, provider, credential_data) "
            "VALUES ($1::uuid, $2, $3) "
            "ON CONFLICT (tenant_id, provider) DO UPDATE SET "
            "    credential_data = EXCLUDED.credential_data, "
            "    updated_at = NOW() "
            "RETURNING provider, created_at, updated_at",
            tenant_id, provider, credential_data,
        )
    assert row is not None
    return CredentialRow(
        provider=row["provider"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def delete_tenant_credential(*, tenant_id: str, provider: str) -> bool:
    """Delete the credential row for ``(tenant_id, provider)``.

    Returns ``True`` iff a row was removed. The route layer is
    responsible for the 409-on-in-use check; this helper does the
    DELETE unconditionally so a concurrent reference race cannot
    leave a half-state.
    """
    async with tenant_conn(tenant_id) as conn:
        status = await conn.execute(
            "DELETE FROM tenant_model_credentials "
            "WHERE tenant_id = $1::uuid AND provider = $2",
            tenant_id, provider,
        )
    return status.endswith(" 1")


async def get_coworker_ids_for_tenant_provider(
    *, tenant_id: str, provider: str
) -> list[str]:
    """Return coworker IDs in ``tenant_id`` whose model uses ``provider``.

    Used to schedule per-coworker ``web.coworker.restart`` events when
    the tenant rewrites its credential — the orchestrator subscriber
    is per-coworker (see :mod:`rolemesh.orchestration.coworker_hot_reload`)
    so we fan out one event per affected coworker rather than
    inventing a tenant-wide event shape.

    Belt-and-braces: ``coworkers`` is RLS-bound on ``tenant_id`` and
    we also include the explicit ``WHERE coworkers.tenant_id`` predicate
    (INV-1). ``models`` is RLS-free but the join column is the
    coworker's ``model_id`` so the constraint already comes from
    the parent.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT c.id "
            "FROM coworkers c "
            "JOIN models m ON m.id = c.model_id "
            "WHERE c.tenant_id = $1::uuid AND m.provider = $2",
            tenant_id, provider,
        )
    return [str(r["id"]) for r in rows]
