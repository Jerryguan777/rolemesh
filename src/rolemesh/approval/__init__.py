"""Orchestrator-side approval module.

Depends on rolemesh.db + NATS transport. Container-side policy
matching lives in ``agent_runner.approval.policy`` and is imported by
both this package's engine and the in-container hook.
"""

from .engine import ApprovalEngine, ChannelSender
from .notification import NotificationTargetResolver
from .types import (
    APPROVAL_STATUSES,
    AUDIT_ACTIONS,
    ApprovalAuditEntry,
    ApprovalPolicy,
    ApprovalRequest,
)

__all__ = [
    "APPROVAL_STATUSES",
    "AUDIT_ACTIONS",
    "ApprovalAuditEntry",
    "ApprovalEngine",
    "ApprovalPolicy",
    "ApprovalRequest",
    "ChannelSender",
    "NotificationTargetResolver",
]
