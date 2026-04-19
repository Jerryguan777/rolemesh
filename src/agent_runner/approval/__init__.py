"""Approval policy matching (shared pure functions).

Imported by both the container-side ApprovalHookHandler and the
orchestrator-side ApprovalEngine. Keep this package free of external
side effects (no DB, no NATS) so the single implementation can run in
either process.
"""

from .policy import (
    compute_action_hash,
    evaluate_condition,
    find_matching_policies_for_actions,
    find_matching_policy,
    select_strictest_policy,
)

__all__ = [
    "compute_action_hash",
    "evaluate_condition",
    "find_matching_policies_for_actions",
    "find_matching_policy",
    "select_strictest_policy",
]
