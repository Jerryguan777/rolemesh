"""FastAPI dependency injection for Admin API authentication and authorization."""

from __future__ import annotations

from fastapi import HTTPException, Request

from rolemesh.auth.permissions import user_can
from rolemesh.auth.provider import AuthenticatedUser
from webui import auth
from webui.config import ADMIN_BOOTSTRAP_TOKEN

# Default tenant ID used for bootstrap token auth
_DEFAULT_TENANT = "default"


async def get_current_user(request: Request) -> AuthenticatedUser:
    """Extract Bearer token and authenticate. Supports bootstrap token for initial setup."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = header[7:]

    # 1. Check bootstrap token first
    if ADMIN_BOOTSTRAP_TOKEN and token == ADMIN_BOOTSTRAP_TOKEN:
        # Resolve the default tenant ID from the database
        from rolemesh.db.pg import get_tenant_by_slug

        tenant = await get_tenant_by_slug("default")
        tenant_id = tenant.id if tenant else _DEFAULT_TENANT
        return AuthenticatedUser(
            user_id="bootstrap",
            tenant_id=tenant_id,
            role="owner",
            name="Bootstrap Admin",
        )

    # 2. Try AuthProvider
    user = await auth.authenticate_request(token)
    if user is not None:
        return user

    raise HTTPException(status_code=401, detail="Invalid or expired token")


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
