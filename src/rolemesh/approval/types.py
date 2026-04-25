"""Dataclasses used across the orchestrator-side approval module.

These mirror the three database tables (approval_policies,
approval_requests, approval_audit_log). CRUD functions in
``rolemesh.db.pg`` return these, and the engine/executor/notification
modules consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalPolicy:
    """A single approval gate for a (server, tool) on a tenant.

    ``coworker_id`` None means the policy applies to every coworker in
    the tenant. Policy snapshots are passed to containers as plain dicts
    so they can share ``agent_runner.approval.policy`` without importing
    this dataclass.
    """

    id: str
    tenant_id: str
    coworker_id: str | None
    mcp_server_name: str
    tool_name: str
    condition_expr: dict[str, Any]
    approver_user_ids: list[str]
    notify_conversation_id: str | None
    auto_expire_minutes: int
    post_exec_mode: str
    enabled: bool
    priority: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the dict shape the container-side policy matcher expects."""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "coworker_id": self.coworker_id,
            "mcp_server_name": self.mcp_server_name,
            "tool_name": self.tool_name,
            "condition_expr": self.condition_expr,
            "approver_user_ids": list(self.approver_user_ids),
            "notify_conversation_id": self.notify_conversation_id,
            "auto_expire_minutes": self.auto_expire_minutes,
            "post_exec_mode": self.post_exec_mode,
            "enabled": self.enabled,
            "priority": self.priority,
            "updated_at": self.updated_at,
        }


@dataclass
class ApprovalRequest:
    """A pending or resolved approval request.

    ``user_id`` is the Agent-turn originator (from AgentInitData.user_id),
    which the Worker uses as the X-RoleMesh-User-Id when executing
    approved actions. It is NOT an approver identity.

    ``action_hashes`` doubles as the MCP idempotency key set (one per
    action, same ordering as ``actions``) and as the 5-minute dedup key
    for auto-intercept requests.
    """

    id: str
    tenant_id: str
    coworker_id: str
    conversation_id: str | None
    policy_id: str
    user_id: str
    job_id: str
    mcp_server_name: str
    actions: list[dict[str, Any]]
    action_hashes: list[str]
    rationale: str | None
    source: str  # "proposal" | "auto_intercept"
    status: str
    post_exec_mode: str
    resolved_approvers: list[str]
    requested_at: str
    expires_at: str
    created_at: str
    updated_at: str


@dataclass
class ApprovalAuditEntry:
    """A single, append-only audit log row.

    ``actor_user_id`` is None for system-generated transitions (created
    via auto-intercept, expired, cancelled, skipped, executing, executed,
    execution_failed, execution_stale). The tests in test_audit_log_actor
    pin these rules so a refactor cannot accidentally record "system"
    actions as if a user took them.
    """

    id: str
    tenant_id: str
    request_id: str
    action: str
    actor_user_id: str | None
    note: str | None
    metadata: dict[str, Any]
    created_at: str


# Valid status transitions (documented for tests, not enforced at DB level —
# atomic SQL does the enforcement where it matters).
APPROVAL_STATUSES: set[str] = {
    "pending",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "skipped",
    "executing",
    "executed",
    "execution_failed",
    "execution_stale",
}

AUDIT_ACTIONS: set[str] = {
    "created",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "skipped",
    "executing",
    "executed",
    "execution_failed",
    "execution_stale",
}


__all__ = [
    "APPROVAL_STATUSES",
    "AUDIT_ACTIONS",
    "ApprovalAuditEntry",
    "ApprovalPolicy",
    "ApprovalRequest",
]


def _empty_dict() -> dict[str, Any]:
    # Small helper to placate mypy when we need a default_factory and a
    # non-empty initial shape is not helpful.
    return {}


# Silences unused-import warnings when `field` is only needed for future
# dataclasses on this module.
_ = field
