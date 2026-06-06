"""FastAPI dependency injection for Admin API authentication and authorization."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, Request

from rolemesh.auth.permissions import user_can
from webui import auth

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

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


def require_action(
    action: str,
) -> Callable[[AuthenticatedUser], Coroutine[None, None, AuthenticatedUser]]:
    """Factory that returns a dependency requiring a specific permission action.

    The returned dependency resolves the caller via ``get_current_user`` as a
    *sub-dependency* (not a direct call) so that FastAPI dependency overrides
    in tests flow through, and so the capability gate composes cleanly with the
    rest of the dependency chain.

    The inner function is tagged with ``_required_action`` so the default-deny
    meta-test (``tests/webui/test_v1_default_deny.py``) can walk a route's
    dependency tree and confirm every ``/api/v1`` mutation is gated. Do not
    rename or drop that attribute without updating the meta-test.
    """

    async def _check(
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> AuthenticatedUser:
        if not user_can(user.role, action):  # type: ignore[arg-type]
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions: requires {action}",
            )
        return user

    # Tag so route introspection (meta-test) can detect the gate and which
    # action it enforces.
    _check._required_action = action  # type: ignore[attr-defined]
    return _check


def require_manage_or_owner(
    *, manage_action: str, resource: object, user: AuthenticatedUser
) -> None:
    """Ownership-escape gate: allow when the caller holds ``manage_action`` OR
    owns the already-loaded ``resource``.

    Called from *inside* a handler (it needs the loaded row), as the complement
    to the route-level capability gate. The route should carry the lowest
    sensible capability (e.g. ``agent.use``); this helper then lets a member
    who created the resource manage their OWN copy while still blocking them
    from others'/shared ones.

    Three-valued logic is load-bearing: a resource whose ``created_by_user_id``
    is NULL is treated as "not mine" — NULL never equals the caller, so such a
    resource falls through to requiring ``manage_action``. This prevents a
    member from claiming ownership of an un-attributed (system/legacy) row.

    Raises 403 when neither the capability nor ownership holds.
    """
    created_by = getattr(resource, "created_by_user_id", None)
    owns = created_by is not None and created_by == user.user_id
    if user_can(user.role, manage_action) or owns:  # type: ignore[arg-type]
        return
    raise HTTPException(
        status_code=403,
        detail=f"Insufficient permissions: requires {manage_action} or resource ownership",
    )


def user_can_see_resource(
    *, manage_action: str, resource: object, user: AuthenticatedUser
) -> bool:
    """Single source of truth for "may this user SEE/USE this resource".

    feat/roles PR3. A resource (coworker or skill) is visible/usable to a
    caller when ANY of the following holds:

    * it is ``shared`` (every tenant member may see and use it), OR
    * the caller created it (``created_by_user_id == user.user_id``), OR
    * the caller holds the manage capability (``manage_action``) — owner /
      admin / platform_admin reach every row regardless of visibility.

    USE is intentionally BROADER than the ``require_manage_or_owner``
    write-gate: a shared resource is usable by everyone, but a member still
    may not *manage* (edit/delete/share) it without the capability or
    ownership. The two helpers therefore differ only in the ``shared``
    clause, and both share the same three-valued-logic treatment of
    ``created_by_user_id IS NULL`` (NULL never equals the caller, so an
    un-attributed private row is invisible to members).

    Every list / fetch / use / skill-bind path routes its visibility
    decision through this helper (the LIST path mirrors the exact same
    predicate in SQL so NULL does not leak — see
    ``rolemesh.db.coworker.get_coworkers_for_tenant``). Keep them in sync.
    """
    visibility = getattr(resource, "visibility", "shared")
    if visibility == "shared":
        return True
    created_by = getattr(resource, "created_by_user_id", None)
    if created_by is not None and created_by == user.user_id:
        return True
    return user_can(user.role, manage_action)  # type: ignore[arg-type]
