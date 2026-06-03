"""Platform-level safety rules — cross-tenant, platform-owned reads.

Rows live in ``platform_safety_rules`` (no ``tenant_id``): they apply
across ALL tenants and are owned by the platform, not any tenant admin.
The loader stamps the running job's ``tenant_id`` onto each snapshot so
the Safety Pipeline — which is left untouched — treats platform and
tenant rules identically.

Two read paths, deliberately on different pools:

  - :func:`fetch_platform_rule_snapshots` — **admin_conn** (B-class
    cross-tenant maintenance per the RLS design). Used by the
    orchestrator-side loader; returns pipeline-ready snapshot dicts for
    every ENABLED platform rule (all tiers, floor included — floor still
    enforces; only its *visibility* is suppressed elsewhere).

  - :func:`list_visible_platform_rules` — **tenant_conn**. Used by the
    tenant-facing v1 read API, which must stay within the business pool
    (webui never imports ``admin_conn``). ``rolemesh_app`` holds SELECT
    on this RLS-free catalog, so a plain ``tenant_conn`` read works; the
    ``tenant_id`` argument only scopes the connection, not the query.
    Floor-tier rows are filtered out here — that is where the three-tier
    visibility contract lives.

This phase exposes no write helpers: the 5 default-tier rules are seeded
via schema seeding (:func:`rolemesh.db.schema._seed_platform_safety_rules`)
and there is no platform-admin write REST yet.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "VISIBLE_TIERS",
    "fetch_platform_rule_snapshots",
    "list_visible_platform_rules",
]

# Tiers a tenant is allowed to SEE. ``floor`` is intentionally absent —
# it enforces but is never surfaced to tenants.
VISIBLE_TIERS: tuple[str, ...] = ("default", "transparent_floor")


def _coerce_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw) if raw else {}
    return raw if isinstance(raw, dict) else {}


def _row_to_snapshot(row: asyncpg.Record, *, tenant_id: str) -> dict[str, Any]:
    """Project a platform rule row onto the pipeline snapshot shape.

    Keys match ``rolemesh.safety.types.Rule.to_snapshot_dict`` exactly so
    the pipeline cannot tell a platform snapshot from a tenant one.
    ``tenant_id`` is the running job's tenant (stamped purely for audit
    attribution); ``coworker_id`` is None (applies to every coworker).
    """
    return {
        "id": str(row["id"]),
        "tenant_id": tenant_id,
        "coworker_id": None,
        "stage": row["stage"],
        "check_id": row["check_id"],
        "config": _coerce_config(row["config"]),
        "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "description": row["description"] or "",
    }


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Full row projection (includes ``tier`` + timestamps) for read APIs."""
    return {
        "id": str(row["id"]),
        "tier": row["tier"],
        "stage": row["stage"],
        "check_id": row["check_id"],
        "config": _coerce_config(row["config"]),
        "priority": int(row["priority"]),
        "enabled": bool(row["enabled"]),
        "description": row["description"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else "",
    }


async def fetch_platform_rule_snapshots(tenant_id: str) -> list[dict[str, Any]]:
    """Enabled platform rules as pipeline-ready snapshot dicts.

    B-class (``admin_conn``): the table is tenant-agnostic. ``tenant_id``
    is stamped onto each snapshot so the pipeline's audit attribution
    lands on the running job's tenant — platform rules apply regardless.
    ALL enabled tiers are returned (floor included): floor still enforces;
    only its visibility is suppressed, and that happens in the read API,
    not here.
    """
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT id, stage, check_id, config, priority, enabled, description
            FROM platform_safety_rules
            WHERE enabled = TRUE
            ORDER BY priority DESC, created_at
            """
        )
    return [_row_to_snapshot(r, tenant_id=tenant_id) for r in rows]


async def list_visible_platform_rules(tenant_id: str) -> list[dict[str, Any]]:
    """Platform rules a tenant is allowed to see (default + transparent_floor).

    Runs on ``tenant_conn`` so the v1 read API stays in the business pool
    (``rolemesh_app`` has SELECT on this RLS-free catalog). ``tenant_id``
    only scopes the connection — platform rules are global, so the query
    has no tenant predicate. Floor-tier rows are never returned.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM platform_safety_rules
            WHERE tier = ANY($1::text[])
            ORDER BY priority DESC, created_at
            """,
            list(VISIBLE_TIERS),
        )
    return [_row_to_dict(r) for r in rows]
