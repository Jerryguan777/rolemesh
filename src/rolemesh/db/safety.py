"""Safety Framework CRUD — rules, decisions, audit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from rolemesh.db._pool import tenant_conn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from rolemesh.safety.types import Rule as SafetyRule


__all__ = [
    "count_safety_decisions",
    "count_safety_rules",
    "count_safety_rules_audit",
    "create_safety_rule",
    "delete_safety_rule",
    "get_safety_decision",
    "get_safety_rule",
    "insert_safety_decision",
    "list_safety_decisions",
    "list_safety_rules",
    "list_safety_rules_audit",
    "list_safety_rules_for_coworker",
    "stream_safety_decisions",
    "update_safety_rule",
]


# ---------------------------------------------------------------------------
# Safety Framework CRUD
# ---------------------------------------------------------------------------


def _record_to_safety_rule(row: asyncpg.Record) -> SafetyRule:
    from rolemesh.safety.types import Rule, Stage

    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg) if cfg else {}
    return Rule(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]) if row["coworker_id"] else None,
        stage=Stage(row["stage"]),
        check_id=row["check_id"],
        config=cfg if isinstance(cfg, dict) else {},
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        description=row["description"] or "",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def _set_safety_guc(
    conn: asyncpg.Connection[asyncpg.Record],
    *,
    actor_user_id: str | None,
) -> None:
    """Set the transaction-local ``safety.actor_user_id`` GUC.

    The audit trigger ``_safety_rules_write_audit_from_trigger``
    reads this to attribute the audit row it emits. Call inside an
    open transaction; the ``is_local=true`` flag auto-clears on
    commit/rollback.
    """
    await conn.execute(
        "SELECT set_config('safety.actor_user_id', $1, true)",
        actor_user_id or "",
    )


async def create_safety_rule(
    *,
    tenant_id: str,
    stage: str,
    check_id: str,
    config: dict[str, Any],
    coworker_id: str | None = None,
    priority: int = 100,
    enabled: bool = True,
    description: str = "",
    actor_user_id: str | None = None,
) -> SafetyRule:
    """Insert a new safety rule and return the stored row.

    ``actor_user_id`` is attributed to the audit row written by the
    trigger. ``None`` is a legitimate value for bulk imports / migration
    scripts where no user is the actor — the audit row carries NULL.
    """
    async with tenant_conn(tenant_id) as conn:
        await _set_safety_guc(conn, actor_user_id=actor_user_id)
        row = await conn.fetchrow(
            """
            INSERT INTO safety_rules (
                tenant_id, coworker_id, stage, check_id,
                config, priority, enabled, description
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5::jsonb, $6, $7, $8
            )
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            stage,
            check_id,
            json.dumps(config),
            priority,
            enabled,
            description,
        )
    assert row is not None
    return _record_to_safety_rule(row)


async def get_safety_rule(
    rule_id: str, *, tenant_id: str
) -> SafetyRule | None:
    """Fetch a safety rule by id, scoped to ``tenant_id``.

    The tenant filter is part of the query (not a post-fetch check) so a
    guessed UUID from another tenant returns None from the DB itself.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM safety_rules "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            rule_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_safety_rule(row)


def _safety_rule_filter_sql(
    tenant_id: str,
    *,
    coworker_id: str | None,
    stage: str | None,
    enabled: bool | None,
) -> tuple[str, list[Any]]:
    """Build the shared WHERE clause for list/count of safety rules."""
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    if stage is not None:
        params.append(stage)
        clauses.append(f"stage = ${len(params)}")
    if enabled is not None:
        params.append(enabled)
        clauses.append(f"enabled = ${len(params)}")
    return " AND ".join(clauses), params


async def list_safety_rules(
    tenant_id: str,
    *,
    coworker_id: str | None = None,
    stage: str | None = None,
    enabled: bool | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[SafetyRule]:
    """List rules for a tenant, optionally filtered and paginated."""
    where, params = _safety_rule_filter_sql(
        tenant_id, coworker_id=coworker_id, stage=stage, enabled=enabled,
    )
    sql = (
        "SELECT * FROM safety_rules WHERE "
        + where
        + " ORDER BY priority DESC, updated_at DESC"
    )
    if limit is not None:
        params.extend((limit, offset))
        sql += f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_safety_rule(r) for r in rows]


async def count_safety_rules(
    tenant_id: str,
    *,
    coworker_id: str | None = None,
    stage: str | None = None,
    enabled: bool | None = None,
) -> int:
    """Total tenant rule count matching the same filters as the list."""
    where, params = _safety_rule_filter_sql(
        tenant_id, coworker_id=coworker_id, stage=stage, enabled=enabled,
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM safety_rules WHERE " + where, *params,
        )
    return int(row["n"]) if row else 0


async def list_safety_rules_for_coworker(
    tenant_id: str, coworker_id: str
) -> list[SafetyRule]:
    """Rules applicable to a specific coworker (coworker-scoped OR tenant-wide).

    Only enabled rows are returned, and a NULL ``coworker_id`` means the
    rule applies to every coworker in the tenant.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM safety_rules
            WHERE tenant_id = $1::uuid
              AND enabled = TRUE
              AND (coworker_id IS NULL OR coworker_id = $2::uuid)
            ORDER BY priority DESC, updated_at DESC
            """,
            tenant_id,
            coworker_id,
        )
    return [_record_to_safety_rule(r) for r in rows]


async def update_safety_rule(
    rule_id: str,
    *,
    tenant_id: str,
    stage: str | None = None,
    check_id: str | None = None,
    config: dict[str, Any] | None = None,
    coworker_id: str | None = None,
    coworker_id_set: bool = False,
    priority: int | None = None,
    enabled: bool | None = None,
    description: str | None = None,
    actor_user_id: str | None = None,
) -> SafetyRule | None:
    """Update selected fields on a rule; returns the new row or None.

    ``coworker_id_set=True`` is required to explicitly set coworker_id
    (including setting it to NULL for a tenant-wide scope); without
    this flag, passing ``coworker_id=None`` is indistinguishable from
    "don't change". This mirrors the three-state Optional convention
    used elsewhere in this module.

    ``actor_user_id`` attributes the audit row. A no-op update (all
    fields unchanged) skips both the DML and the audit row — the
    trigger's ``IF v_before <> v_after`` guard does the filtering.
    """
    fields: list[str] = []
    values: list[Any] = []
    idx = 1

    def _push(expr: str, value: Any) -> None:
        nonlocal idx
        fields.append(expr.format(i=idx))
        values.append(value)
        idx += 1

    if stage is not None:
        _push("stage = ${i}", stage)
    if check_id is not None:
        _push("check_id = ${i}", check_id)
    if config is not None:
        _push("config = ${i}::jsonb", json.dumps(config))
    if coworker_id_set:
        _push("coworker_id = ${i}::uuid", coworker_id)
    if priority is not None:
        _push("priority = ${i}", priority)
    if enabled is not None:
        _push("enabled = ${i}", enabled)
    if description is not None:
        _push("description = ${i}", description)

    if not fields:
        return await get_safety_rule(rule_id, tenant_id=tenant_id)

    fields.append("updated_at = now()")
    values.append(rule_id)
    values.append(tenant_id)
    sql = (
        "UPDATE safety_rules SET "
        + ", ".join(fields)
        + f" WHERE id = ${idx}::uuid AND tenant_id = ${idx + 1}::uuid "
        "RETURNING *"
    )
    async with tenant_conn(tenant_id) as conn:
        await _set_safety_guc(conn, actor_user_id=actor_user_id)
        row = await conn.fetchrow(sql, *values)
    if row is None:
        return None
    return _record_to_safety_rule(row)


async def delete_safety_rule(
    rule_id: str, *, tenant_id: str, actor_user_id: str | None = None
) -> bool:
    """Hard-delete a rule scoped to ``tenant_id``. Returns True if a
    row was removed.

    The audit trigger captures the row's pre-delete state in
    before_state so the deleted rule is reconstructable forever.
    """
    async with tenant_conn(tenant_id) as conn:
        await _set_safety_guc(conn, actor_user_id=actor_user_id)
        result = await conn.execute(
            "DELETE FROM safety_rules "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            rule_id,
            tenant_id,
        )
    return result.endswith(" 1")


def _safety_audit_filter_sql(
    tenant_id: str, *, rule_id: str | None,
) -> tuple[str, list[Any]]:
    """Shared WHERE clause for the audit list/count."""
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if rule_id is not None:
        params.append(rule_id)
        clauses.append(f"rule_id = ${len(params)}::uuid")
    return " AND ".join(clauses), params


async def count_safety_rules_audit(
    *, tenant_id: str, rule_id: str | None = None,
) -> int:
    """Total audit-row count matching the same filters as the list."""
    where, params = _safety_audit_filter_sql(tenant_id, rule_id=rule_id)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM safety_rules_audit WHERE " + where,
            *params,
        )
    return int(row["n"]) if row else 0


async def list_safety_rules_audit(
    *,
    tenant_id: str,
    rule_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List rule-change audit rows, newest first (paginated).

    Filtered by tenant_id (never cross-tenant). ``rule_id`` optional
    to narrow to a specific rule's history. Returns plain dicts; the
    admin UI surfaces this as a timeline.
    """
    where, params = _safety_audit_filter_sql(tenant_id, rule_id=rule_id)
    params.append(limit)
    limit_pos = len(params)
    params.append(offset)
    sql = (
        "SELECT * FROM safety_rules_audit WHERE "
        + where
        + f" ORDER BY created_at DESC LIMIT ${limit_pos} OFFSET ${len(params)}"
    )
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    result: list[dict[str, Any]] = []
    for r in rows:
        before = r["before_state"]
        after = r["after_state"]
        if isinstance(before, str):
            before = json.loads(before) if before else None
        if isinstance(after, str):
            after = json.loads(after) if after else None
        result.append(
            {
                "id": str(r["id"]),
                "rule_id": str(r["rule_id"]),
                "tenant_id": str(r["tenant_id"]),
                "action": r["action"],
                "actor_user_id": str(r["actor_user_id"])
                if r["actor_user_id"]
                else None,
                "before_state": before,
                "after_state": after,
                "created_at": r["created_at"].isoformat()
                if r["created_at"]
                else "",
            }
        )
    return result


async def insert_safety_decision(
    *,
    tenant_id: str,
    stage: str,
    verdict_action: str,
    triggered_rule_ids: list[str],
    findings: list[dict[str, Any]],
    context_digest: str,
    context_summary: str,
    coworker_id: str | None = None,
    conversation_id: str | None = None,
    job_id: str | None = None,
) -> str:
    """Write one audit row; return its id.

    Called by the safety_events subscriber for every decision the
    container publishes. Never raises on per-row validation — malformed
    inputs should be filtered upstream in ``SafetyEngine.handle_safety_event``.

    ``source`` ('tenant' | 'platform') is derived here — NOT in the
    pipeline — by checking whether any triggered rule id belongs to the
    platform-owned ``platform_safety_rules`` catalog. ``rolemesh_app``
    holds SELECT on that RLS-free table, so this is a single cheap EXISTS
    inside the same connection. Keeping the derivation at the write layer
    is what lets ``pipeline_core`` stay byte-unchanged.
    """
    async with tenant_conn(tenant_id) as conn:
        source = "tenant"
        if triggered_rule_ids:
            is_platform = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM platform_safety_rules "
                "WHERE id = ANY($1::uuid[]))",
                triggered_rule_ids,
            )
            if is_platform:
                source = "platform"
        row = await conn.fetchrow(
            """
            INSERT INTO safety_decisions (
                tenant_id, coworker_id, conversation_id, job_id,
                stage, verdict_action, triggered_rule_ids,
                findings, context_digest, context_summary, source
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5, $6, $7::uuid[],
                $8::jsonb, $9, $10, $11
            )
            RETURNING id
            """,
            tenant_id,
            coworker_id,
            conversation_id,
            job_id,
            stage,
            verdict_action,
            triggered_rule_ids,
            json.dumps(findings),
            context_digest,
            context_summary,
            source,
        )
    assert row is not None
    return str(row["id"])


async def stream_safety_decisions(
    tenant_id: str,
    *,
    from_ts: str | None = None,
    to_ts: str | None = None,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    stage: str | None = None,
    chunk_size: int = 1000,
) -> AsyncIterator[list[dict[str, Any]]]:
    """Yield rows in ``chunk_size`` batches for streaming CSV export.

    Uses an asyncpg cursor inside a transaction so 100k-row exports
    don't pull the entire result set into memory. Each chunk is a
    flat list of dicts with the same shape as ``list_safety_decisions``
    (caller picks which fields to put on the CSV row).

    ``from_ts`` / ``to_ts`` are ISO-8601 strings coerced to
    ``timestamptz`` inside the query so operators can write
    ``"2026-04-01"`` or ``"2026-04-01T00:00:00+00:00"`` interchangeably.
    Malformed timestamps raise at query time (psycopg surface) which
    the REST layer turns into 422.
    """
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if from_ts is not None:
        params.append(from_ts)
        clauses.append(f"created_at >= ${len(params)}::timestamptz")
    if to_ts is not None:
        params.append(to_ts)
        clauses.append(f"created_at <= ${len(params)}::timestamptz")
    if verdict_action is not None:
        params.append(verdict_action)
        clauses.append(f"verdict_action = ${len(params)}")
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    if stage is not None:
        params.append(stage)
        clauses.append(f"stage = ${len(params)}")
    sql = (
        "SELECT id, created_at, tenant_id, coworker_id, "
        "conversation_id, job_id, stage, verdict_action, "
        "triggered_rule_ids, findings, context_summary "
        "FROM safety_decisions WHERE "
        + " AND ".join(clauses)
        + " ORDER BY created_at DESC"
    )
    async with tenant_conn(tenant_id) as conn:
        cur = await conn.cursor(sql, *params)
        while True:
            rows = await cur.fetch(chunk_size)
            if not rows:
                return
            chunk: list[dict[str, Any]] = []
            for r in rows:
                findings = r["findings"]
                if isinstance(findings, str):
                    findings = json.loads(findings) if findings else []
                chunk.append(
                    {
                        "id": str(r["id"]),
                        "created_at": (
                            r["created_at"].isoformat()
                            if r["created_at"]
                            else ""
                        ),
                        "tenant_id": str(r["tenant_id"]),
                        "coworker_id": (
                            str(r["coworker_id"])
                            if r["coworker_id"]
                            else None
                        ),
                        "conversation_id": r["conversation_id"],
                        "job_id": r["job_id"],
                        "stage": r["stage"],
                        "verdict_action": r["verdict_action"],
                        "triggered_rule_ids": [
                            str(u) for u in (r["triggered_rule_ids"] or [])
                        ],
                        "findings": (
                            findings if isinstance(findings, list) else []
                        ),
                        "context_summary": r["context_summary"],
                    }
                )
            yield chunk


async def get_safety_decision(
    decision_id: str, *, tenant_id: str
) -> dict[str, Any] | None:
    """Fetch a single safety_decisions row, scoped to ``tenant_id``.

    Tenant scoping is on the query (not a post-fetch check) so a leak
    path like "admin from tenant B fetches tenant A's row via a guessed
    UUID" returns None from the DB itself. The REST layer then maps
    None to 404 — indistinguishable from "doesn't exist" so we don't
    leak UUID existence across tenants.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM safety_decisions WHERE id = $1::uuid "
            "AND tenant_id = $2::uuid",
            decision_id,
            tenant_id,
        )
    if row is None:
        return None
    findings = row["findings"]
    if isinstance(findings, str):
        findings = json.loads(findings) if findings else []
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "coworker_id": str(row["coworker_id"]) if row["coworker_id"] else None,
        "conversation_id": row["conversation_id"],
        "job_id": row["job_id"],
        "stage": row["stage"],
        "verdict_action": row["verdict_action"],
        "triggered_rule_ids": [str(u) for u in (row["triggered_rule_ids"] or [])],
        "findings": findings if isinstance(findings, list) else [],
        "context_digest": row["context_digest"],
        "context_summary": row["context_summary"],
        "source": row["source"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
    }


def _safety_decision_where_clauses(
    tenant_id: str,
    *,
    verdict_action: str | None,
    coworker_id: str | None,
    stage: str | None,
    from_ts: str | None,
    to_ts: str | None,
    check_id: str | None = None,
    rule_id: str | None = None,
) -> tuple[str, list[Any]]:
    """Shared WHERE-clause builder for list + count calls.

    ``rule_id`` matches decisions whose ``triggered_rule_ids`` array
    contains that rule (``@>`` array-contains).

    ``check_id`` has no direct column on ``safety_decisions`` — a
    decision records the *rules* it triggered, and ``check_id`` lives on
    the rule. So we translate the check into its rule ids and test for
    array overlap (``&&``). The ids may belong to either the tenant's own
    ``safety_rules`` (RLS-scoped to the caller within this connection, so
    a foreign tenant's rule can never resolve here) or the global,
    read-only ``platform_safety_rules`` catalog — a triggered rule can be
    platform-owned, so both must be considered.
    """
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if verdict_action is not None:
        params.append(verdict_action)
        clauses.append(f"verdict_action = ${len(params)}")
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    if stage is not None:
        params.append(stage)
        clauses.append(f"stage = ${len(params)}")
    if from_ts is not None:
        params.append(from_ts)
        clauses.append(f"created_at >= ${len(params)}::timestamptz")
    if to_ts is not None:
        params.append(to_ts)
        clauses.append(f"created_at <= ${len(params)}::timestamptz")
    if rule_id is not None:
        params.append(rule_id)
        clauses.append(f"triggered_rule_ids @> ARRAY[${len(params)}::uuid]")
    if check_id is not None:
        params.append(check_id)
        n = len(params)
        clauses.append(
            "triggered_rule_ids && ("
            f"ARRAY(SELECT id FROM safety_rules WHERE check_id = ${n}) || "
            f"ARRAY(SELECT id FROM platform_safety_rules WHERE check_id = ${n})"
            ")"
        )
    return " AND ".join(clauses), params


async def count_safety_decisions(
    tenant_id: str,
    *,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    stage: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    check_id: str | None = None,
    rule_id: str | None = None,
) -> int:
    """Total count for a matching filter set.

    Split from ``list_safety_decisions`` so the REST pagination path
    can make two parallel calls (count + page) without paying for the
    count on internal read-the-latest-N call sites. Matches the filter
    arg set of the list function so drift is a local concern.
    """
    where, params = _safety_decision_where_clauses(
        tenant_id,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_ts,
        to_ts=to_ts,
        check_id=check_id,
        rule_id=rule_id,
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchval(
            f"SELECT COUNT(*) FROM safety_decisions WHERE {where}",
            *params,
        )
    return int(row or 0)


async def list_safety_decisions(
    tenant_id: str,
    *,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    stage: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    check_id: str | None = None,
    rule_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Read safety decisions for a tenant, newest first.

    Pagination via ``limit`` + ``offset``. Callers that need a total
    count for UI pagination pair this with ``count_safety_decisions``
    using the same filter args. The two-call surface keeps internal
    "read the latest N" callers from paying for a count scan.
    """
    where, params = _safety_decision_where_clauses(
        tenant_id,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_ts,
        to_ts=to_ts,
        check_id=check_id,
        rule_id=rule_id,
    )
    params.append(limit)
    params.append(offset)
    sql = (
        f"SELECT * FROM safety_decisions WHERE {where} "
        f"ORDER BY created_at DESC LIMIT ${len(params) - 1} "
        f"OFFSET ${len(params)}"
    )
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    result: list[dict[str, Any]] = []
    for r in rows:
        findings = r["findings"]
        if isinstance(findings, str):
            findings = json.loads(findings) if findings else []
        result.append(
            {
                "id": str(r["id"]),
                "tenant_id": str(r["tenant_id"]),
                "coworker_id": str(r["coworker_id"]) if r["coworker_id"] else None,
                "conversation_id": r["conversation_id"],
                "job_id": r["job_id"],
                "stage": r["stage"],
                "verdict_action": r["verdict_action"],
                "triggered_rule_ids": [str(u) for u in (r["triggered_rule_ids"] or [])],
                "findings": findings if isinstance(findings, list) else [],
                "context_digest": r["context_digest"],
                "context_summary": r["context_summary"],
                "source": r["source"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            }
        )
    return result


