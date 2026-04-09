"""FastAPI dependency injection for Admin API authentication and authorization."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from rolemesh.auth.permissions import user_can
from webui import auth

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser


async def get_current_user(request: Request) -> AuthenticatedUser:
    """Extract Bearer token and authenticate via AuthProvider or bootstrap token."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = header[7:]

    user = await auth.authenticate_ws(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def require_action(action: str):
    """Factory that returns a dependency requiring a specific permission action."""

    async def _check(request: Request) -> AuthenticatedUser:
        user = await get_current_user(request)
        if not user_can(user.role, action):  # type: ignore[arg-type]
            raise HTTPException(status_code=403, detail=f"Insufficient permissions: requires {action}")
        return user

    return _check


# Pre-built dependencies for common use
require_manage_tenant = require_action("manage_tenant")  # owner only
require_manage_agents = require_action("manage_agents")  # admin+
require_manage_users = require_action("manage_users")  # admin+
