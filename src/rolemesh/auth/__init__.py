"""RoleMesh authentication and authorization.

TokenService is not re-exported here to avoid pulling in PyJWT on every
import.  Use ``from rolemesh.auth.token_service import TokenService``
when needed.
"""

from rolemesh.auth.authorization import (
    can_delegate,
    can_manage_task,
    can_schedule_task,
    can_see_data,
)
from rolemesh.auth.permissions import (
    AGENT_DEFAULTS,
    SUPER_AGENT_DEFAULTS,
    AgentPermissions,
    AgentRole,
    UserRole,
    user_can,
)
from rolemesh.auth.provider import AuthenticatedUser, AuthProvider

__all__ = [
    "AGENT_DEFAULTS",
    "SUPER_AGENT_DEFAULTS",
    "AgentPermissions",
    "AgentRole",
    "AuthProvider",
    "AuthenticatedUser",
    "UserRole",
    "can_delegate",
    "can_manage_task",
    "can_schedule_task",
    "can_see_data",
    "user_can",
]
