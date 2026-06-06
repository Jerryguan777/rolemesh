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

Write helpers (:func:`create_platform_rule`, :func:`update_platform_rule`,
:func:`set_platform_rule_enabled`, :func:`delete_platform_rule`) and the
all-tiers reads (:func:`list_all_platform_rules`, :func:`get_platform_rule`)
back the platform-admin REST surface (``/api/v1/platform/safety/rules``).
They run on **admin_conn**: ``rolemesh_app`` holds SELECT but never
INSERT/UPDATE/DELETE on this catalog (see schema GRANTs), so writes MUST
use the cross-tenant maintenance pool. Unlike the tenant-facing read,
these surface ALL tiers (floor included) — the platform operator manages
floor; only its *visibility* is suppressed for tenants.

The 5 default-tier rules are still seeded at build time
(:func:`rolemesh.db.schema._seed_platform_safety_rules`) and carry
``is_seeded = TRUE``; they are managed disable-only (the write API
forbids hard-deleting them — a delete would be undone by the next seed).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "VISIBLE_TIERS",
    "create_platform_rule",
    "delete_platform_rule",
    "fetch_platform_rule_snapshots",
    "get_platform_rule",
    "list_all_platform_rules",
    "list_visible_platform_rules",
    "set_platform_rule_enabled",
    "update_platform_rule",
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
        "is_seeded": bool(row["is_seeded"]),
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


# ---------------------------------------------------------------------------
# Platform-admin management (admin_conn) — all tiers, including floor.
# ---------------------------------------------------------------------------


async def list_all_platform_rules() -> list[dict[str, Any]]:
    """Every platform rule across ALL tiers (floor included).

    For the platform-admin surface — the operator manages floor too, so
    (unlike :func:`list_visible_platform_rules`) nothing is filtered.
    Runs on ``admin_conn``: this is platform-plane management, not a
    tenant read.
    """
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM platform_safety_rules
            ORDER BY tier, priority DESC, created_at
            """
        )
    return [_row_to_dict(r) for r in rows]


async def get_platform_rule(rule_id: str) -> dict[str, Any] | None:
    """One platform rule by id, any tier (or None). ``admin_conn``."""
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM platform_safety_rules WHERE id = $1", rule_id
        )
    return _row_to_dict(row) if row is not None else None


async def create_platform_rule(
    *,
    tier: str,
    stage: str,
    check_id: str,
    config: dict[str, Any],
    priority: int,
    description: str,
) -> dict[str, Any]:
    """Insert a platform-admin-created rule (``is_seeded = FALSE``).

    ``admin_conn`` — the business role cannot write this catalog. The
    UNIQUE ``(tier, check_id, stage)`` identity is enforced by the DB; a
    duplicate raises ``asyncpg.UniqueViolationError`` for the caller to
    map to a 409.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO platform_safety_rules
                (tier, stage, check_id, config, priority, description,
                 is_seeded)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, FALSE)
            RETURNING *
            """,
            tier,
            stage,
            check_id,
            json.dumps(config),
            priority,
            description,
        )
    # INSERT ... RETURNING always yields a row; the assert narrows the
    # asyncpg ``Record | None`` type for the projector.
    assert row is not None
    return _row_to_dict(row)


async def update_platform_rule(
    rule_id: str,
    *,
    config: dict[str, Any] | None = None,
    priority: int | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    """Patch a platform rule's mutable fields (or None if no such id).

    Only ``config`` / ``priority`` / ``description`` / ``enabled`` are
    mutable — ``tier`` / ``stage`` / ``check_id`` form the rule identity
    and are immutable (change = create a new rule). Each ``None`` argument
    leaves its column untouched via ``COALESCE``. Seeded defaults are
    editable here; ``is_seeded`` only gates DELETE, not edits. ``admin_conn``.
    """
    config_json = json.dumps(config) if config is not None else None
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            UPDATE platform_safety_rules
            SET config      = COALESCE($2::jsonb, config),
                priority    = COALESCE($3, priority),
                description = COALESCE($4, description),
                enabled     = COALESCE($5, enabled),
                updated_at  = now()
            WHERE id = $1
            RETURNING *
            """,
            rule_id,
            config_json,
            priority,
            description,
            enabled,
        )
    return _row_to_dict(row) if row is not None else None


async def set_platform_rule_enabled(
    rule_id: str, *, enabled: bool
) -> dict[str, Any] | None:
    """Toggle a platform rule's ``enabled`` flag (or None if no such id).

    Backs the dedicated enable/disable endpoints; disable is also the
    sanctioned way to suppress a seeded default. ``admin_conn``.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            UPDATE platform_safety_rules
            SET enabled = $2, updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            rule_id,
            enabled,
        )
    return _row_to_dict(row) if row is not None else None


async def delete_platform_rule(rule_id: str) -> bool:
    """Hard-delete a platform rule; True if a row was removed.

    Does NOT itself enforce the seeded-default guard — the REST layer
    fetches first and refuses (409) when ``is_seeded`` so it can return a
    clear "disable instead" message rather than a silent miss. ``admin_conn``.
    """
    async with admin_conn() as conn:
        result = await conn.execute(
            "DELETE FROM platform_safety_rules WHERE id = $1", rule_id
        )
    # asyncpg returns the command tag, e.g. "DELETE 1" / "DELETE 0";
    # the id is a PK so at most one row is ever affected.
    return result == "DELETE 1"
