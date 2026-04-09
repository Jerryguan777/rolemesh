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
# Agent roles & permissions
# ---------------------------------------------------------------------------

AgentRole = Literal["super_agent", "agent"]


@dataclass(frozen=True)
class AgentPermissions:
    """The 4 agent permission fields.

    * data_scope   — "tenant" (see all coworkers' data) or "self" (own only).
    * task_schedule — whether the agent can create scheduled tasks.
    * task_manage_others — whether the agent can manage other agents' tasks.
    * agent_delegate — whether the agent can invoke other agents.
    """

    data_scope: Literal["tenant", "self"] = "self"
    task_schedule: bool = False
    task_manage_others: bool = False
    agent_delegate: bool = False

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "data_scope": self.data_scope,
            "task_schedule": self.task_schedule,
            "task_manage_others": self.task_manage_others,
            "agent_delegate": self.agent_delegate,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> AgentPermissions:
        scope = d.get("data_scope", "self")
        if scope not in ("tenant", "self"):
            scope = "self"
        return cls(
            data_scope=scope,  # type: ignore[arg-type]
            task_schedule=bool(d.get("task_schedule", False)),
            task_manage_others=bool(d.get("task_manage_others", False)),
            agent_delegate=bool(d.get("agent_delegate", False)),
        )

    # -- Role templates ------------------------------------------------------

    @classmethod
    def for_role(cls, role: str) -> AgentPermissions:
        """Return default permissions for the given agent role.

        Accepts "super_agent" or "agent". Unknown roles default to agent.
        """
        if role == "super_agent":
            return SUPER_AGENT_DEFAULTS
        return AGENT_DEFAULTS


SUPER_AGENT_DEFAULTS = AgentPermissions(
    data_scope="tenant",
    task_schedule=True,
    task_manage_others=True,
    agent_delegate=True,
)

AGENT_DEFAULTS = AgentPermissions()
