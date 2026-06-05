"""HITL tool-approval CRUD — policies and requests.

Tenant-scoped reads/writes go through ``tenant_conn`` (RLS-bound); the
two genuinely cross-tenant maintenance reads used by the orchestrator's
expiry watcher and restart recovery go through ``admin_conn`` and say so
in their docstrings (docs/21-hitl-approval-plan.md §4 / §8).

The persistence shape is the frozen contract in §4. The DB is
authoritative — the orchestrator's in-memory suspend state is a cache
rebuilt from ``status='pending'`` rows on restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_runner.approval.policy import ApprovalPolicy
from rolemesh.db._pool import admin_conn, tenant_conn

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg


__all__ = [
    "ApprovalRequest",
    "create_approval_policy",
    "create_approval_request",
    "delete_approval_policy",
    "get_approval_policy",
    "get_approval_request",
    "list_approval_policies",
    "list_pending_requests_all_tenants",
    "list_pending_requests_for_tenant",
    "list_requests_for_conversation",
    "resolve_approval_request",
    "update_approval_policy",
]


# Terminal statuses an approval request can be resolved into. ``pending``
# is the only non-terminal state; a row transitions out of it exactly once.
_RESOLVED_STATUSES = frozenset({"approved", "rejected", "expired", "cancelled"})


@dataclass(frozen=True)
class ApprovalRequest:
    """A persisted approval request (§4.2)."""

    id: str
    tenant_id: str
    coworker_id: str
    conversation_id: str | None
    policy_id: str | None
    user_id: str | None
    job_id: str
    mcp_server_name: str
    action: dict[str, Any]          # { tool_name, params }
    action_summary: str | None
    rationale: str | None           # agent's "why" (nullable; no fill yet)
    # Safety->approval provenance {kind, rule_id, check_id, stage}; None for a
    # business-policy approval. Set by the safety hook bridge (§3.10).
    triggered_by: dict[str, Any] | None
    status: str
    decided_by: str | None
    note: str | None
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None


def _json_to_dict(value: Any) -> dict[str, Any]:
    """Normalise a jsonb column (asyncpg yields ``str`` without a codec)."""
    if isinstance(value, str):
        value = json.loads(value) if value else {}
    return value if isinstance(value, dict) else {}


def _json_to_optional_dict(value: Any) -> dict[str, Any] | None:
    """Like :func:`_json_to_dict` but preserves NULL as ``None``.

    Used for nullable jsonb columns (``triggered_by``) where the absence of a
    value is semantically distinct from an empty object.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value) if value else None
    return value if isinstance(value, dict) else None


def _record_to_policy(row: asyncpg.Record) -> ApprovalPolicy:
    return ApprovalPolicy(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        mcp_server_name=row["mcp_server_name"],
        tool_name=row["tool_name"],
        condition_expr=_json_to_dict(row["condition_expr"]),
        enabled=bool(row["enabled"]),
        priority=int(row["priority"]),
        updated_at=row["updated_at"],
        created_at=row["created_at"],
    )


def _record_to_request(row: asyncpg.Record) -> ApprovalRequest:
    return ApprovalRequest(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        policy_id=str(row["policy_id"]) if row["policy_id"] else None,
        user_id=str(row["user_id"]) if row["user_id"] else None,
        job_id=row["job_id"],
        mcp_server_name=row["mcp_server_name"],
        action=_json_to_dict(row["action"]),
        action_summary=row["action_summary"],
        rationale=row["rationale"],
        triggered_by=_json_to_optional_dict(row["triggered_by"]),
        status=row["status"],
        decided_by=str(row["decided_by"]) if row["decided_by"] else None,
        note=row["note"],
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
    )


# ---------------------------------------------------------------------------
# approval_policies
# ---------------------------------------------------------------------------


async def create_approval_policy(
    *,
    tenant_id: str,
    mcp_server_name: str,
    tool_name: str,
    condition_expr: dict[str, Any] | None = None,
    enabled: bool = True,
    priority: int = 0,
) -> ApprovalPolicy:
    """Insert a policy and return the stored row.

    ``condition_expr`` defaults to ``{"always": true}`` (every call to the
    matched tool needs approval) — the conservative default for a gate.
    """
    expr = condition_expr if condition_expr is not None else {"always": True}
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO approval_policies (
                tenant_id, mcp_server_name, tool_name,
                condition_expr, enabled, priority
            )
            VALUES ($1::uuid, $2, $3, $4::jsonb, $5, $6)
            RETURNING *
            """,
            tenant_id,
            mcp_server_name,
            tool_name,
            json.dumps(expr),
            enabled,
            priority,
        )
    assert row is not None
    return _record_to_policy(row)


async def get_approval_policy(
    policy_id: str, *, tenant_id: str
) -> ApprovalPolicy | None:
    """Fetch a policy by id, scoped to ``tenant_id`` in the query itself."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_policies "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            policy_id,
            tenant_id,
        )
    return _record_to_policy(row) if row else None


async def list_approval_policies(
    tenant_id: str, *, enabled_only: bool = False
) -> list[ApprovalPolicy]:
    """List a tenant's policies.

    ``enabled_only=True`` returns the snapshot the matcher consumes
    (``find_matching_policy``). Order is the matcher's tiebreak order
    (priority desc, then newest) so callers that take the first match get
    the same answer as the matcher — but the matcher does not rely on it.
    """
    sql = "SELECT * FROM approval_policies WHERE tenant_id = $1::uuid"
    if enabled_only:
        sql += " AND enabled = TRUE"
    sql += " ORDER BY priority DESC, updated_at DESC"
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, tenant_id)
    return [_record_to_policy(r) for r in rows]


async def update_approval_policy(
    policy_id: str,
    *,
    tenant_id: str,
    mcp_server_name: str | None = None,
    tool_name: str | None = None,
    condition_expr: dict[str, Any] | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
) -> ApprovalPolicy | None:
    """Update selected fields; returns the new row or ``None`` if absent."""
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
    return _record_to_policy(row) if row else None


async def delete_approval_policy(policy_id: str, *, tenant_id: str) -> bool:
    """Hard-delete a policy scoped to ``tenant_id``. True if a row went."""
    async with tenant_conn(tenant_id) as conn:
        result = await conn.execute(
            "DELETE FROM approval_policies "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            policy_id,
            tenant_id,
        )
    return result.endswith(" 1")


# ---------------------------------------------------------------------------
# approval_requests
# ---------------------------------------------------------------------------


async def create_approval_request(
    *,
    tenant_id: str,
    coworker_id: str,
    job_id: str,
    mcp_server_name: str,
    action: dict[str, Any],
    expires_at: datetime,
    conversation_id: str | None = None,
    policy_id: str | None = None,
    user_id: str | None = None,
    action_summary: str | None = None,
    rationale: str | None = None,
    triggered_by: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ApprovalRequest:
    """Insert a ``pending`` approval request and return it.

    ``action`` is the ``{tool_name, params}`` snapshot; the request is the
    source of truth for what gets approved, not a live re-read of the tool
    call. ``user_id`` is the approver (the task creator). A ``None``
    approver is persisted as-is so the caller can fail closed on it.

    ``triggered_by`` is the safety-rule provenance {kind, rule_id, check_id,
    stage} when the request was raised by the safety pipeline's
    require_approval bridge; ``None`` for a business-policy approval.

    ``request_id`` lets the caller pin the row's primary key. The container
    mints the ``request_id`` it blocks on (§3.1) *before* the orchestrator
    persists, and the decision relay routes back by that same id (§3.2), so the
    row id MUST equal the container's request_id — otherwise the approve/reject
    relay could never find the awaiting call. Left ``None`` (e.g. S1 CRUD
    tests) the DB default mints a fresh id.
    """
    triggered_by_json = json.dumps(triggered_by) if triggered_by is not None else None
    async with tenant_conn(tenant_id) as conn:
        if request_id is not None:
            row = await conn.fetchrow(
                """
                INSERT INTO approval_requests (
                    id, tenant_id, coworker_id, conversation_id, policy_id,
                    user_id, job_id, mcp_server_name, action, action_summary,
                    rationale, triggered_by, expires_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                    $6::uuid, $7, $8, $9::jsonb, $10, $11, $12::jsonb, $13
                )
                RETURNING *
                """,
                request_id,
                tenant_id,
                coworker_id,
                conversation_id,
                policy_id,
                user_id,
                job_id,
                mcp_server_name,
                json.dumps(action),
                action_summary,
                rationale,
                triggered_by_json,
                expires_at,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO approval_requests (
                    tenant_id, coworker_id, conversation_id, policy_id, user_id,
                    job_id, mcp_server_name, action, action_summary, rationale,
                    triggered_by, expires_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                    $6, $7, $8::jsonb, $9, $10, $11::jsonb, $12
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
                json.dumps(action),
                action_summary,
                rationale,
                triggered_by_json,
                expires_at,
            )
    assert row is not None
    return _record_to_request(row)


async def get_approval_request(
    request_id: str, *, tenant_id: str
) -> ApprovalRequest | None:
    """Fetch a request by id, scoped to ``tenant_id`` in the query."""
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_requests "
            "WHERE id = $1::uuid AND tenant_id = $2::uuid",
            request_id,
            tenant_id,
        )
    return _record_to_request(row) if row else None


async def list_pending_requests_for_tenant(
    tenant_id: str,
) -> list[ApprovalRequest]:
    """Pending requests for one tenant (oldest first)."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM approval_requests "
            "WHERE tenant_id = $1::uuid AND status = 'pending' "
            "ORDER BY requested_at ASC",
            tenant_id,
        )
    return [_record_to_request(r) for r in rows]


async def list_requests_for_conversation(
    conversation_id: str, *, tenant_id: str
) -> list[ApprovalRequest]:
    """All approval requests for one conversation, oldest first.

    Unlike :func:`list_pending_requests_for_tenant` this returns every status
    (pending + resolved) so the web chat can re-render the conversation's full
    approval record inline. Tenant-scoped via ``tenant_conn`` (RLS) plus the
    explicit ``tenant_id`` predicate (INV-1 belt).
    """
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM approval_requests "
            "WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid "
            "ORDER BY requested_at ASC",
            tenant_id,
            conversation_id,
        )
    return [_record_to_request(r) for r in rows]


async def list_pending_requests_all_tenants() -> list[ApprovalRequest]:
    """Every pending request across all tenants (oldest first).

    Cross-tenant by design (class B maintenance): the orchestrator's
    restart recovery and expiry watcher must scan all live containers'
    pending approvals regardless of tenant. Goes through ``admin_conn``
    (BYPASSRLS). Never call this from a request handler.
    """
    # inv-1-ok: deliberate cross-tenant maintenance scan (restart recovery /
    # expiry watcher) — a tenant_id predicate would defeat the purpose.
    async with admin_conn() as conn:
        rows = await conn.fetch(
            "SELECT * FROM approval_requests "
            "WHERE status = 'pending' ORDER BY requested_at ASC"
        )
    return [_record_to_request(r) for r in rows]


async def resolve_approval_request(
    request_id: str,
    *,
    tenant_id: str,
    status: str,
    decided_by: str | None = None,
    note: str | None = None,
) -> ApprovalRequest | None:
    """Transition a *pending* request to a terminal status, idempotently.

    The ``WHERE ... status = 'pending'`` clause makes the transition
    first-wins: a late approval click that races a timeout-expiry (or a
    double click) updates zero rows the second time and returns ``None``.
    Both sides of the §8 decision race converge on the first writer.

    Returns the updated row on a successful transition, or ``None`` if the
    request was absent or already resolved.
    """
    if status not in _RESOLVED_STATUSES:
        raise ValueError(f"not a terminal status: {status!r}")
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = $3, decided_by = $4::uuid, note = $5,
                decided_at = now()
            WHERE id = $1::uuid AND tenant_id = $2::uuid
              AND status = 'pending'
            RETURNING *
            """,
            request_id,
            tenant_id,
            status,
            decided_by,
            note,
        )
    return _record_to_request(row) if row else None
