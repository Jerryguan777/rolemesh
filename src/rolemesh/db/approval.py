"""Approval policies, requests, and audit log."""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Any

from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

    from rolemesh.approval.types import (
        ApprovalAuditEntry,
        ApprovalPolicy,
        ApprovalRequest,
    )


__all__ = [
    "DecisionOutcome",
    "cancel_pending_approvals_for_job",
    "claim_approval_for_execution",
    "create_approval_policy",
    "create_approval_request",
    "decide_approval_request_full",
    "delete_approval_policy",
    "expire_approval_if_pending",
    "find_pending_request_by_action_hash",
    "get_approval_policy",
    "get_approval_request",
    "get_enabled_policies_for_coworker",
    "list_approval_audit",
    "list_approval_policies",
    "list_approval_requests",
    "list_expired_pending_approvals",
    "list_stuck_approved_approvals",
    "list_stuck_executing_approvals",
    "resolve_request_tenant",
    "set_approval_status",
    "update_approval_policy",
    "write_approval_audit",
]


# ---------------------------------------------------------------------------
# Approval policies CRUD
# ---------------------------------------------------------------------------


def _record_to_approval_policy(row: asyncpg.Record) -> ApprovalPolicy:
    from rolemesh.approval.types import ApprovalPolicy

    cond = row["condition_expr"]
    if isinstance(cond, str):
        cond = json.loads(cond) if cond else {}
    approvers = row["approver_user_ids"] or []
    return ApprovalPolicy(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]) if row["coworker_id"] else None,
        mcp_server_name=row["mcp_server_name"],
        tool_name=row["tool_name"],
        condition_expr=cond if isinstance(cond, dict) else {},
        approver_user_ids=[str(a) for a in approvers],
        notify_conversation_id=str(row["notify_conversation_id"])
        if row["notify_conversation_id"]
        else None,
        auto_expire_minutes=row["auto_expire_minutes"] or 60,
        post_exec_mode=row["post_exec_mode"] or "report",
        enabled=bool(row["enabled"]),
        priority=row["priority"] or 0,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def create_approval_policy(
    *,
    tenant_id: str,
    mcp_server_name: str,
    tool_name: str,
    condition_expr: dict[str, Any],
    coworker_id: str | None = None,
    approver_user_ids: list[str] | None = None,
    notify_conversation_id: str | None = None,
    auto_expire_minutes: int = 60,
    post_exec_mode: str = "report",
    enabled: bool = True,
    priority: int = 0,
) -> ApprovalPolicy:
    """Insert a new approval policy and return the stored row."""
    approvers = approver_user_ids or []
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO approval_policies (
                tenant_id, coworker_id, mcp_server_name, tool_name,
                condition_expr, approver_user_ids, notify_conversation_id,
                auto_expire_minutes, post_exec_mode, enabled, priority
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5::jsonb, $6::uuid[], $7::uuid,
                $8, $9, $10, $11
            )
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            mcp_server_name,
            tool_name,
            json.dumps(condition_expr),
            approvers,
            notify_conversation_id,
            auto_expire_minutes,
            post_exec_mode,
            enabled,
            priority,
        )
    assert row is not None
    return _record_to_approval_policy(row)


async def get_approval_policy(
    policy_id: str, *, tenant_id: str
) -> ApprovalPolicy | None:
    """Fetch a policy by id, scoped to ``tenant_id``.

    The tenant filter is enforced at the SQL layer so a forged or
    guessed UUID from another tenant returns None instead of leaking
    the policy. Callers that already validated tenant ownership at
    a higher layer can keep their existing checks; this function is
    the lower bound, not the only line of defense.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_policies "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            policy_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_approval_policy(row)


async def list_approval_policies(
    tenant_id: str,
    *,
    coworker_id: str | None = None,
    enabled: bool | None = None,
) -> list[ApprovalPolicy]:
    """List policies for a tenant, optionally filtered by coworker and state."""
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    if enabled is not None:
        params.append(enabled)
        clauses.append(f"enabled = ${len(params)}")
    sql = (
        "SELECT * FROM approval_policies WHERE "
        + " AND ".join(clauses)
        + " ORDER BY priority DESC, updated_at DESC"
    )
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_approval_policy(r) for r in rows]


async def get_enabled_policies_for_coworker(
    tenant_id: str, coworker_id: str
) -> list[ApprovalPolicy]:
    """Policies applicable to a specific coworker.

    Includes both coworker-scoped policies (coworker_id matches) and
    tenant-wide policies (coworker_id IS NULL). Only returns enabled
    rows — container snapshots never carry disabled policies, and
    neither does the engine's dedup/match path.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_policies
            WHERE tenant_id = $1::uuid
              AND enabled = TRUE
              AND (coworker_id IS NULL OR coworker_id = $2::uuid)
            ORDER BY priority DESC, updated_at DESC
            """,
            tenant_id,
            coworker_id,
        )
    return [_record_to_approval_policy(r) for r in rows]


async def update_approval_policy(
    policy_id: str,
    *,
    tenant_id: str,
    mcp_server_name: str | None = None,
    tool_name: str | None = None,
    condition_expr: dict[str, Any] | None = None,
    approver_user_ids: list[str] | None = None,
    notify_conversation_id: str | None = None,
    auto_expire_minutes: int | None = None,
    post_exec_mode: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
) -> ApprovalPolicy | None:
    """Update selected fields on a policy; returns the new row or None.

    Both the SELECT (no-fields path) and the UPDATE filter on
    ``tenant_id``, so a forged policy_id from another tenant has no
    effect.
    """
    fields: list[str] = []
    values: list[Any] = []
    idx = 1

    def _push(expr: str, value: Any) -> None:
        nonlocal idx
        fields.append(expr.format(i=idx))
        values.append(value)
        idx += 1

    if mcp_server_name is not None:
        _push("mcp_server_name = ${i}", mcp_server_name)
    if tool_name is not None:
        _push("tool_name = ${i}", tool_name)
    if condition_expr is not None:
        _push("condition_expr = ${i}::jsonb", json.dumps(condition_expr))
    if approver_user_ids is not None:
        _push("approver_user_ids = ${i}::uuid[]", approver_user_ids)
    if notify_conversation_id is not None:
        _push("notify_conversation_id = ${i}::uuid", notify_conversation_id)
    if auto_expire_minutes is not None:
        _push("auto_expire_minutes = ${i}", auto_expire_minutes)
    if post_exec_mode is not None:
        _push("post_exec_mode = ${i}", post_exec_mode)
    if enabled is not None:
        _push("enabled = ${i}", enabled)
    if priority is not None:
        _push("priority = ${i}", priority)

    if not fields:
        return await get_approval_policy(policy_id, tenant_id=tenant_id)

    fields.append("updated_at = now()")
    values.append(policy_id)
    values.append(tenant_id)
    sql = (
        "UPDATE approval_policies SET "
        + ", ".join(fields)
        + f" WHERE id = ${idx}::uuid AND tenant_id = ${idx + 1}::uuid "
        "RETURNING *"
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(sql, *values)
    if row is None:
        return None
    return _record_to_approval_policy(row)


async def delete_approval_policy(policy_id: str, *, tenant_id: str) -> bool:
    """Hard-delete a policy scoped to ``tenant_id``. Returns True if
    a row was removed.

    A delete request for a policy id that belongs to another tenant
    returns False (no rows affected) without raising — same shape as
    "policy id does not exist".
    """
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM approval_policies "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            policy_id,
            tenant_id,
        )
    return result.endswith(" 1")


# ---------------------------------------------------------------------------
# Approval requests CRUD
# ---------------------------------------------------------------------------


def _record_to_approval_request(row: asyncpg.Record) -> ApprovalRequest:
    from rolemesh.approval.types import ApprovalRequest

    actions = row["actions"]
    if isinstance(actions, str):
        actions = json.loads(actions) if actions else []
    hashes = row["action_hashes"] or []
    approvers = row["resolved_approvers"] or []
    return ApprovalRequest(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        policy_id=str(row["policy_id"]),
        user_id=str(row["user_id"]),
        job_id=row["job_id"],
        mcp_server_name=row["mcp_server_name"],
        actions=list(actions) if isinstance(actions, list) else [],
        action_hashes=list(hashes),
        rationale=row["rationale"],
        source=row["source"],
        status=row["status"],
        post_exec_mode=row["post_exec_mode"] or "report",
        resolved_approvers=[str(a) for a in approvers],
        requested_at=row["requested_at"].isoformat() if row["requested_at"] else "",
        expires_at=row["expires_at"].isoformat() if row["expires_at"] else "",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def _set_approval_guc(
    conn: asyncpg.Connection[asyncpg.Record],
    *,
    actor_user_id: str | None,
    note: str | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Set approval.* transaction-local GUCs for the audit trigger.

    The audit trigger (_approval_write_audit_from_trigger) reads these
    to attribute the audit row it emits. Call inside an open transaction;
    the ``is_local=true`` flag auto-clears on commit/rollback.
    """
    await conn.execute(
        "SELECT set_config('approval.actor_user_id', $1, true)",
        actor_user_id or "",
    )
    await conn.execute(
        "SELECT set_config('approval.note', $1, true)",
        note or "",
    )
    await conn.execute(
        "SELECT set_config('approval.metadata', $1, true)",
        json.dumps(metadata) if metadata else "",
    )


async def create_approval_request(
    *,
    tenant_id: str,
    coworker_id: str,
    conversation_id: str | None,
    policy_id: str | None,
    user_id: str,
    job_id: str,
    mcp_server_name: str,
    actions: list[dict[str, Any]],
    action_hashes: list[str],
    rationale: str | None,
    source: str,
    status: str,
    resolved_approvers: list[str],
    expires_at: datetime,
    post_exec_mode: str = "report",
    actor_user_id: str | None = None,
) -> ApprovalRequest:
    """Insert a new approval request row.

    ``actor_user_id`` is recorded by the audit trigger on the 'created'
    row. None ⇒ audit 'created' row has NULL actor (system-initiated).
    """
    async with tenant_conn(tenant_id) as conn:
        await _set_approval_guc(
            conn, actor_user_id=actor_user_id, note=None, metadata=None
        )
        row = await conn.fetchrow(
            """
            INSERT INTO approval_requests (
                tenant_id, coworker_id, conversation_id, policy_id,
                user_id, job_id, mcp_server_name,
                actions, action_hashes, rationale, source, status,
                post_exec_mode, resolved_approvers, expires_at
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                $5::uuid, $6, $7,
                $8::jsonb, $9::text[], $10, $11, $12,
                $13, $14::uuid[], $15
            )
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            conversation_id,
            policy_id,
            user_id,
            job_id,
            mcp_server_name,
            json.dumps(actions),
            list(action_hashes),
            rationale,
            source,
            status,
            post_exec_mode,
            list(resolved_approvers),
            expires_at,
        )
    assert row is not None
    return _record_to_approval_request(row)


async def resolve_request_tenant(request_id: str) -> str | None:
    """Look up a request's tenant_id by request_id alone.

    System-only escape hatch. The single legitimate caller is the
    approval Worker's NATS message handler, which needs to recover
    tenant_id when processing legacy messages published before the
    tenant-id-in-body protocol was introduced. The Worker is a trusted
    orchestrator-internal process and the request_id is taken from
    the orchestrator-published NATS subject, so trusting the row's
    own tenant_id is acceptable here.

    DO NOT use this from REST handlers or any code path that consumes
    user-controlled ids. The return value carries authority — pair it
    with a tenant-scoped get_approval_request call once you have it.

    Returns None if the request_id does not exist.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM approval_requests WHERE id = $1::uuid",
            request_id,
        )
    if row is None:
        return None
    return str(row["tenant_id"])


async def get_approval_request(
    request_id: str, *, tenant_id: str
) -> ApprovalRequest | None:
    """Fetch a request by id, scoped to ``tenant_id``.

    See ``get_approval_policy`` for the tenant-filter rationale.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_requests "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            request_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


async def list_approval_requests(
    tenant_id: str,
    *,
    status: str | None = None,
    coworker_id: str | None = None,
    limit: int = 100,
) -> list[ApprovalRequest]:
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if status is not None:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    params.append(limit)
    sql = (
        "SELECT * FROM approval_requests WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_approval_request(r) for r in rows]


async def find_pending_request_by_action_hash(
    tenant_id: str, action_hash: str, within_minutes: int = 5
) -> ApprovalRequest | None:
    """Dedup key for auto-intercept: return the most recent pending
    request whose action_hashes array contains ``action_hash`` and was
    created within the last ``within_minutes``.

    This prevents the hook chain from creating two pending requests
    when an agent retries the same blocked tool call seconds apart.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM approval_requests
            WHERE tenant_id = $1::uuid
              AND status = 'pending'
              AND $2 = ANY(action_hashes)
              AND created_at > now() - ($3 || ' minutes')::interval
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tenant_id,
            action_hash,
            str(within_minutes),
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


class DecisionOutcome:
    """Return value of decide_approval_request_full.

    One of three shapes:
      updated    — the actual UPDATE landed; ``request`` is the new row.
      conflict   — the request was not pending; ``current_status`` says why.
      forbidden  — the request is pending but the caller is not an approver.
    """

    __slots__ = ("current_status", "kind", "request")

    def __init__(
        self,
        kind: str,
        request: ApprovalRequest | None = None,
        current_status: str | None = None,
    ) -> None:
        self.kind = kind  # "updated" | "conflict" | "forbidden" | "missing"
        self.request = request
        self.current_status = current_status


async def decide_approval_request_full(
    request_id: str,
    *,
    tenant_id: str,
    new_status: str,
    actor_user_id: str,
    note: str | None = None,
) -> DecisionOutcome:
    """Single-query decide that disambiguates 403 vs 409 vs 200 vs 404.

    Uses a CTE: we capture the pre-UPDATE status first, run the
    conditional UPDATE in the same statement, and return both — one
    round trip instead of two, and no race window where the status
    changes between two separate reads.

    Both the pre-UPDATE SELECT and the UPDATE filter on
    ``tenant_id``, so a forged request_id from another tenant is
    indistinguishable from "request not found" — DecisionOutcome
    kind="missing".

    Also sets the GUCs inside the same transaction so the audit trigger
    records the approver as the actor_user_id on the 'approved' /
    'rejected' row.
    """
    async with tenant_conn(tenant_id) as conn:
        await _set_approval_guc(
            conn, actor_user_id=actor_user_id, note=note, metadata=None
        )
        row = await conn.fetchrow(
            """
            WITH before AS (
                SELECT id, status, resolved_approvers
                FROM approval_requests
                WHERE id = $2::uuid AND tenant_id = $4::uuid
                FOR UPDATE
            ),
            upd AS (
                UPDATE approval_requests r
                SET status = $1, updated_at = now()
                FROM before b
                WHERE r.id = b.id
                  AND b.status = 'pending'
                  AND $3::uuid = ANY(b.resolved_approvers)
                RETURNING r.*
            )
            SELECT
                (SELECT row_to_json(upd) FROM upd) AS updated_row,
                (SELECT status FROM before) AS before_status,
                (SELECT $3::uuid = ANY(resolved_approvers) FROM before) AS is_approver
            """,
            new_status,
            request_id,
            actor_user_id,
            tenant_id,
        )
    if row is None:
        return DecisionOutcome(kind="missing")
    before_status = row["before_status"]
    if before_status is None:
        return DecisionOutcome(kind="missing")
    updated_raw = row["updated_row"]
    if updated_raw is not None:
        if isinstance(updated_raw, str):
            updated_raw = json.loads(updated_raw)
        # row_to_json strips column types; fetch the real row to get
        # datetime objects decoded correctly.
        updated = await get_approval_request(request_id, tenant_id=tenant_id)
        return DecisionOutcome(kind="updated", request=updated)
    if before_status != "pending":
        return DecisionOutcome(kind="conflict", current_status=before_status)
    # pending but UPDATE did not land → caller is not an approver.
    return DecisionOutcome(kind="forbidden", current_status=before_status)


async def claim_approval_for_execution(
    request_id: str, *, tenant_id: str
) -> ApprovalRequest | None:
    """Atomic claim: approved → executing, scoped to ``tenant_id``.

    The Worker uses this to take exclusive ownership before hitting the
    MCP server. If two Workers race, only one sees the row returned; the
    other gets None and must drop the NATS message.

    The audit trigger writes the 'executing' audit row with NULL actor
    (system transition).
    """
    async with tenant_conn(tenant_id) as conn:
        # No actor: the Worker is a system process, so the trigger writes
        # executing with NULL actor.
        await _set_approval_guc(
            conn, actor_user_id=None, note=None, metadata=None
        )
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = 'executing', updated_at = now()
            WHERE id = $1::uuid
              AND tenant_id = $2::uuid
              AND status = 'approved'
            RETURNING *
            """,
            request_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


async def set_approval_status(
    request_id: str,
    status: str,
    *,
    tenant_id: str,
    actor_user_id: str | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ApprovalRequest | None:
    """Unconditional status update scoped to ``tenant_id``. Used for
    system transitions that do not race (e.g. executing → executed
    by the Worker that already holds the claim).

    ``actor_user_id`` / ``note`` / ``metadata`` flow through to the
    audit trigger's 'status-change' row.
    """
    async with tenant_conn(tenant_id) as conn:
        await _set_approval_guc(
            conn,
            actor_user_id=actor_user_id,
            note=note,
            metadata=metadata,
        )
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = $1, updated_at = now()
            WHERE id = $2::uuid AND tenant_id = $3::uuid
            RETURNING *
            """,
            status,
            request_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


async def cancel_pending_approvals_for_job(
    job_id: str,
) -> list[tuple[str, str]]:
    """Move all pending approvals for a job_id to 'cancelled'.

    Returns ``(id, tenant_id)`` tuples for each row that transitioned,
    so the caller can re-fetch the row scoped to the right tenant and
    notify approvers without trusting the bare request id.

    job_id is globally unique (orchestrator-issued, includes coworker
    folder + epoch), so this UPDATE remains job-scoped without an
    explicit tenant filter; the tenant_id we return is the
    authoritative value from the row itself.
    """
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            UPDATE approval_requests
            SET status = 'cancelled', updated_at = now()
            WHERE job_id = $1 AND status = 'pending'
            RETURNING id, tenant_id
            """,
            job_id,
        )
    return [(str(r["id"]), str(r["tenant_id"])) for r in rows]


async def list_expired_pending_approvals() -> list[ApprovalRequest]:
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_requests
            WHERE status = 'pending' AND expires_at < now()
            ORDER BY expires_at
            """
        )
    return [_record_to_approval_request(r) for r in rows]


async def list_stuck_approved_approvals(
    older_than_seconds: int = 60,
) -> list[ApprovalRequest]:
    """Approved rows that have been sitting for a while without being
    claimed by a Worker — either the Worker missed the NATS publish or
    the orchestrator restarted mid-flight. The reconciler republishes
    these."""
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_requests
            WHERE status = 'approved'
              AND updated_at < now() - ($1 || ' seconds')::interval
            ORDER BY updated_at
            """,
            str(older_than_seconds),
        )
    return [_record_to_approval_request(r) for r in rows]


async def list_stuck_executing_approvals(
    older_than_seconds: int = 300,
) -> list[ApprovalRequest]:
    """Executing rows that never transitioned — a Worker probably crashed
    after claiming but before writing the terminal status. The reconciler
    marks them execution_stale rather than retrying, because we cannot
    tell whether the MCP-side work partially completed."""
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_requests
            WHERE status = 'executing'
              AND updated_at < now() - ($1 || ' seconds')::interval
            ORDER BY updated_at
            """,
            str(older_than_seconds),
        )
    return [_record_to_approval_request(r) for r in rows]


# ---------------------------------------------------------------------------
# Approval audit log (append-only)
# ---------------------------------------------------------------------------


def _record_to_audit_entry(row: asyncpg.Record) -> ApprovalAuditEntry:
    from rolemesh.approval.types import ApprovalAuditEntry

    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta) if meta else {}
    return ApprovalAuditEntry(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        request_id=str(row["request_id"]),
        action=row["action"],
        actor_user_id=str(row["actor_user_id"]) if row["actor_user_id"] else None,
        note=row["note"],
        metadata=meta if isinstance(meta, dict) else {},
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )


async def write_approval_audit(
    *,
    tenant_id: str,
    request_id: str,
    action: str,
    actor_user_id: str | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ApprovalAuditEntry:
    """Append a single audit row. There is deliberately no update or
    delete counterpart — the whole point of the audit table is that
    rows are immutable once written.

    ``tenant_id`` is required and stored on the audit row so that audit
    reads can be filtered by tenant without a JOIN. Callers must pass
    the parent request's tenant_id; mismatches will not be detected
    here (FK only validates request_id existence) but reads via
    ``list_approval_audit`` filter on tenant_id, so a mis-attributed
    row would simply be invisible to its rightful tenant.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO approval_audit_log
                (tenant_id, request_id, action, actor_user_id, note, metadata)
            VALUES ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6::jsonb)
            RETURNING *
            """,
            tenant_id,
            request_id,
            action,
            actor_user_id,
            note,
            json.dumps(metadata or {}),
        )
    assert row is not None
    return _record_to_audit_entry(row)


async def list_approval_audit(
    request_id: str, *, tenant_id: str
) -> list[ApprovalAuditEntry]:
    """Return audit rows for a request, scoped to ``tenant_id``.

    Filtering by tenant_id at the SQL layer means a forged or guessed
    request_id from another tenant returns an empty list rather than
    leaking audit history. The composite index
    ``idx_audit_log_tenant_request`` keeps this fast.
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM approval_audit_log "
            "WHERE request_id = $1::uuid AND tenant_id = $2::uuid "
            "ORDER BY created_at ASC",
            request_id,
            tenant_id,
        )
    return [_record_to_audit_entry(r) for r in rows]


async def expire_approval_if_pending(
    request_id: str, *, tenant_id: str
) -> ApprovalRequest | None:
    """Atomic pending → expired scoped to ``tenant_id``, with the CAS
    guard kept in one place.

    Separate from set_approval_status because the maintenance loop
    must not trample a concurrent decide_approval_request.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = 'expired', updated_at = now()
            WHERE id = $1::uuid
              AND tenant_id = $2::uuid
              AND status = 'pending'
            RETURNING *
            """,
            request_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


