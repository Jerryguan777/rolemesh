"""Pure authorization functions used at interception points.

No DB access, no side effects — just yes/no decisions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.permissions import AgentPermissions


def can_schedule_task(permissions: AgentPermissions) -> bool:
    """Can this agent create scheduled tasks?"""
    return permissions.task_schedule


def can_manage_task(
    permissions: AgentPermissions,
    task_coworker_id: str,
    self_coworker_id: str,
) -> bool:
    """Can this agent manage (pause/resume/cancel/update) the given task?

    Own tasks are always manageable; others' tasks require task_manage_others.
    """
    if task_coworker_id == self_coworker_id:
        return True
    return permissions.task_manage_others


def can_see_data(
    permissions: AgentPermissions,
    data_coworker_id: str,
    self_coworker_id: str,
) -> bool:
    """Can this agent see data belonging to *data_coworker_id*?"""
    if permissions.data_scope == "tenant":
        return True
    return data_coworker_id == self_coworker_id


def can_delegate(permissions: AgentPermissions) -> bool:
    """Can this agent invoke other agents?"""
    return permissions.agent_delegate
