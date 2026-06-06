"""Permission model for RoleMesh agents and users.

Agent permissions are a flat 4-field model attached to each coworker.
User roles define what a human user can do within the AaaS platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# User roles
# ---------------------------------------------------------------------------

UserRole = Literal["platform_admin", "owner", "admin", "member"]

# Tenant-plane role -> action capability table.
#
# Fine-grained ``<resource>.<verb>`` actions gate the ``/api/v1/*`` surface.
# Mutations on shared/infra resources require the matching ``*.manage`` /
# ``*.configure`` capability; an ownership-escape (``require_manage_or_owner``)
# lets a member act on their OWN resource even without the capability.
#
# ``platform_admin`` is intentionally absent from this literal table — it is
# derived below as a superset so it can never silently drift out of date when a
# new action is added to any tenant role.
_TENANT_ROLE_ACTIONS: dict[str, set[str]] = {
    "owner": {
        "agent.create",
        "agent.manage",
        "agent.use",
        "skill.create",
        "skill.manage",
        "mcp.configure",
        "approval_policy.manage",
        "credential.byok.manage",
        "safety.read",
        "safety.rule.manage",
        "user.manage",
        "tenant.manage",
        "task.manage",
    },
    "admin": {
        # Admin lacks BYOK credential management and tenant settings (both
        # owner-only) per the §3 role matrix.
        "agent.create",
        "agent.manage",
        "agent.use",
        "skill.create",
        "skill.manage",
        "mcp.configure",
        "approval_policy.manage",
        "safety.read",
        "safety.rule.manage",
        "user.manage",
        "task.manage",
    },
    "member": {
        # A member may create and use agents/skills, and (via the
        # ownership-escape helper) manage the ones they created — but the
        # ``*.manage`` capability that reaches others'/shared resources is
        # withheld here.
        "agent.create",
        "agent.use",
        "skill.create",
    },
}

# Platform-plane-only actions (not granted to any tenant role). Empty today;
# kept as an explicit seam so future platform-wide capabilities are added in
# one obvious place.
_PLATFORM_ONLY_ACTIONS: set[str] = set()


def _all_known_actions() -> set[str]:
    """Union of every action referenced by any tenant role + platform-only."""
    actions: set[str] = set(_PLATFORM_ONLY_ACTIONS)
    for role_actions in _TENANT_ROLE_ACTIONS.values():
        actions |= role_actions
    return actions


# ``platform_admin`` is the superset of every action: every tenant-role action
# plus any platform-only action. Derived (not hand-copied) so it cannot rot —
# adding an action to any role above automatically grants it to platform_admin.
_USER_ROLE_ACTIONS: dict[str, set[str]] = {
    **_TENANT_ROLE_ACTIONS,
    "platform_admin": _all_known_actions(),
}


def user_can(role: UserRole, action: str) -> bool:
    """Check if a user role permits a given action. Fail-closed.

    Unknown roles and unknown actions deny by default
    (``_USER_ROLE_ACTIONS.get(role, set())`` yields the empty set).
    ``platform_admin`` is the superset of all known actions.
    """
    return action in _USER_ROLE_ACTIONS.get(role, set())


# ---------------------------------------------------------------------------
# Agent permissions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentPermissions:
    """Flat agent capability bits. Default is least-privilege (all False).

    * task_schedule — whether the agent can create scheduled tasks.
    * task_manage_others — whether the agent can manage (pause/cancel/update)
      other agents' tasks; also implies seeing other agents' tasks in the
      task snapshot (manage requires visibility).
    * agent_delegate — whether the agent can invoke other agents (reserved
      for a future frontdesk agent; not yet enabled).
    """

    task_schedule: bool = False
    task_manage_others: bool = False
    agent_delegate: bool = False

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "task_schedule": self.task_schedule,
            "task_manage_others": self.task_manage_others,
            "agent_delegate": self.agent_delegate,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> AgentPermissions:
        return cls(
            task_schedule=bool(d.get("task_schedule", False)),
            task_manage_others=bool(d.get("task_manage_others", False)),
            agent_delegate=bool(d.get("agent_delegate", False)),
        )
