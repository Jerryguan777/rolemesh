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

UserRole = Literal["owner", "admin", "member"]

_USER_ROLE_ACTIONS: dict[str, set[str]] = {
    "owner": {
        "manage_tenant",
        "manage_agents",
        "manage_users",
        "view_all_conversations",
        "use_agent",
    },
    "admin": {
        "manage_agents",
        "manage_users",
        "view_all_conversations",
        "use_agent",
    },
    "member": {
        "use_agent",
    },
}


def user_can(role: UserRole, action: str) -> bool:
    """Check if a user role permits a given action.

    Actions: manage_tenant, manage_agents, manage_users,
             view_all_conversations, use_agent.
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
