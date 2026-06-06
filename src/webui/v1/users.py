"""``/api/v1/users`` REST surface — tenant user management.

Equivalence migration of the legacy ``/api/admin/users`` endpoints
(``webui.admin``) onto the ``/api/v1`` conventions: the same DB calls
and response shapes, but gated by the fine-grained ``user.manage``
capability, returning the design §13 error envelope, and treating
cross-tenant rows as ``404`` (never ``403`` — existence is not leaked).

Only ``owner`` may create or assign the ``owner`` role; that business
rule is enforced in-handler (the route-level gate is the broader
``user.manage`` which both ``owner`` and ``admin`` hold).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response

from rolemesh import db
from webui.dependencies import require_action
from webui.schemas import (
    UserCreate,
    UserDetailResponse,
    UserPage,
    UserResponse,
    UserUpdate,
)
from webui.v1._pagination import DEFAULT_PAGE_LIMIT, LimitParam, OffsetParam
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser
    from rolemesh.core.types import User

router = APIRouter(prefix="/users", tags=["Users"])


def _user_to_response(u: User) -> UserResponse:
    return UserResponse(
        id=u.id,
        tenant_id=u.tenant_id,
        name=u.name,
        email=u.email,
        role=u.role,
        channel_ids=u.channel_ids,
        created_at=u.created_at,
    )


@router.get("", response_model=UserPage)
async def list_users(
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
    offset: OffsetParam = 0,
    user: AuthenticatedUser = Depends(require_action("user.manage")),
) -> UserPage:
    users = await db.get_users_for_tenant(
        user.tenant_id, limit=limit, offset=offset,
    )
    total = await db.count_users_for_tenant(user.tenant_id)
    return UserPage(
        items=[_user_to_response(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    user: AuthenticatedUser = Depends(require_action("user.manage")),
) -> UserResponse:
    if body.role == "owner" and user.role != "owner":
        raise_error_response(
            "FORBIDDEN",
            "Only owners can create owner-role users.",
            status_code=403,
        )
    new_user = await db.create_user(
        tenant_id=user.tenant_id,
        name=body.name,
        email=body.email,
        role=body.role,
        channel_ids=body.channel_ids or None,
    )
    return _user_to_response(new_user)


@router.get("/{user_id}", response_model=UserDetailResponse)
async def get_user_detail(
    user_id: str,
    user: AuthenticatedUser = Depends(require_action("user.manage")),
) -> UserDetailResponse:
    target = await db.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise_error_response("NOT_FOUND", "User not found.", status_code=404)
    return UserDetailResponse(**_user_to_response(target).model_dump())


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdate,
    user: AuthenticatedUser = Depends(require_action("user.manage")),
) -> UserResponse:
    target = await db.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise_error_response("NOT_FOUND", "User not found.", status_code=404)
    if body.role == "owner" and user.role != "owner":
        raise_error_response(
            "FORBIDDEN",
            "Only owners can assign owner role.",
            status_code=403,
        )
    updated = await db.update_user(
        user_id,
        tenant_id=user.tenant_id,
        name=body.name,
        email=body.email,
        role=body.role,
    )
    if updated is None:
        raise_error_response("NOT_FOUND", "User not found.", status_code=404)
    return _user_to_response(updated)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    user: AuthenticatedUser = Depends(require_action("user.manage")),
) -> Response:
    if user_id == user.user_id:
        raise_error_response(
            "INVALID_REQUEST",
            "Cannot delete yourself.",
            status_code=400,
        )
    target = await db.get_user(user_id, tenant_id=user.tenant_id)
    if target is None:
        raise_error_response("NOT_FOUND", "User not found.", status_code=404)
    await db.delete_user(user_id, tenant_id=user.tenant_id)
    return Response(status_code=204)
