"""``models`` + ``tenant_model_credentials`` read helpers.

The ``models`` table is tenant-agnostic (no RLS) — every tenant
shares the platform catalog. ``tenant_model_credentials`` is
tenant-scoped; reads go through ``tenant_conn`` so the same
belt-and-braces RLS + WHERE predicate pattern used elsewhere applies.

Only the read paths needed by the v1.1 Phase-1 coworker create /
update validation chain live here. The admin write surface for
custom models is deferred (design §14).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg


__all__ = [
    "ModelRow",
    "get_model_by_id",
    "tenant_has_credential_for_provider",
]


@dataclass(frozen=True, slots=True)
class ModelRow:
    """Minimal projection of a ``models`` row.

    Lean on purpose: every caller in v1.1 Phase 1 only needs
    ``(provider, model_family)`` to run the backend compatibility
    check. Add fields as concrete consumers appear; don't pre-emptively
    surface columns just because they exist.
    """

    id: str
    provider: str
    model_id: str
    model_family: str
    display_name: str
    is_active: bool


def _record_to_model(row: asyncpg.Record) -> ModelRow:
    return ModelRow(
        id=str(row["id"]),
        provider=row["provider"],
        model_id=row["model_id"],
        model_family=row["model_family"],
        display_name=row["display_name"],
        is_active=bool(row["is_active"]),
    )


async def get_model_by_id(model_id: str) -> ModelRow | None:
    """Return the platform model row or ``None``.

    Uses the admin pool because ``models`` has no RLS (every tenant
    sees every row); going through ``tenant_conn`` would still work
    but adds a pointless GUC transaction.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, provider, model_id, model_family, display_name, is_active "
            "FROM models WHERE id = $1::uuid",
            model_id,
        )
    if row is None:
        return None
    return _record_to_model(row)


async def tenant_has_credential_for_provider(
    tenant_id: str, provider: str
) -> bool:
    """Return ``True`` iff the tenant has a credential row for ``provider``.

    Belt-and-braces: even though RLS is enabled on
    ``tenant_model_credentials``, the explicit ``WHERE tenant_id``
    predicate satisfies INV-1. The ``tenant_id`` filter is required
    here, not merely defensive — the RLS GUC must be set inside the
    transaction, and the policy compares against it; both layers must
    line up for a row to be visible.
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
