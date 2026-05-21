"""``/api/v1/approval-policies`` REST surface (design §3 Phase 3).

Stand-alone router that owns the policy CRUD. The legacy
``/api/admin/approval-policies`` endpoints remain in
:mod:`webui.admin` for the 6-month compatibility window; both
surfaces share the same DB helpers and DB-level RLS, so a write
through either path is immediately visible to the other.

``policy_id`` on ``approval_requests`` is ``ON DELETE SET NULL`` —
the design §3 DELETE 语义 table mandates that deleting a policy
must not block already-issued pending requests. The DB schema
already enforces it; this surface relies on that contract rather
than reimplementing the cascade.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.auth.permissions import user_can
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_approval_policy,
    delete_approval_policy,
    get_approval_policy,
    list_approval_policies,
    update_approval_policy,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import (
    ApprovalPolicy,
    ApprovalPolicyCreate,
    ApprovalPolicyUpdate,
)
from webui.v1.errors import raise_error_response

router = APIRouter(prefix="/approval-policies", tags=["ApprovalPolicies"])


def _to_response(p: object) -> ApprovalPolicy:
    return ApprovalPolicy(
        id=p.id,
        tenant_id=p.tenant_id,
        coworker_id=p.coworker_id,
        mcp_server_name=p.mcp_server_name,
        tool_name=p.tool_name,
        condition_expr=p.condition_expr,
        approver_user_ids=list(p.approver_user_ids),
        notify_conversation_id=p.notify_conversation_id,
        auto_expire_minutes=p.auto_expire_minutes,
        post_exec_mode=p.post_exec_mode,  # type: ignore[arg-type]
        enabled=p.enabled,
        priority=p.priority,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _require_manage(user: AuthenticatedUser) -> None:
    """Policy write requires ``manage_agents`` (admin+).

    Reads are unrestricted within the tenant — a member needs to
    see why their proposal is being held; only mutation requires
    elevated role.
    """
    if not user_can(user.role, "manage_agents"):
        raise_error_response(
            "FORBIDDEN",
            "Managing approval policies requires admin+ role.",
            status_code=403,
            details={"required": "manage_agents"},
        )


async def _get_or_404(policy_id: str, *, tenant_id: str) -> object:
    try:
        row = await get_approval_policy(policy_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "Approval policy not found.",
            status_code=404,
            details={"policy_id": policy_id},
        )
    return row


@router.get("", response_model=list[ApprovalPolicy])
async def list_policies(
    coworker_id: str | None = None,
    enabled: bool | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalPolicy]:
    rows = await list_approval_policies(
        user.tenant_id, coworker_id=coworker_id, enabled=enabled
    )
    return [_to_response(r) for r in rows]


@router.post("", response_model=ApprovalPolicy, status_code=201)
async def create_policy(
    body: ApprovalPolicyCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalPolicy:
    _require_manage(user)
    # Cross-tenant guard: if the caller pins a coworker_id, verify it
    # belongs to the active tenant. The DB schema enforces tenant
    # alignment via RLS; this lets the API answer with a deterministic
    # 422 instead of relying on the constraint violation surface.
    if body.coworker_id is not None:
        from rolemesh.db import get_coworker

        try:
            cw = await get_coworker(body.coworker_id, tenant_id=user.tenant_id)
        except asyncpg.DataError:
            cw = None
        if cw is None:
            raise_error_response(
                "INVALID_COWORKER",
                "coworker_id does not belong to this tenant.",
                status_code=422,
                details={"coworker_id": body.coworker_id},
            )
    p = await create_approval_policy(
        tenant_id=user.tenant_id,
        coworker_id=body.coworker_id,
        mcp_server_name=body.mcp_server_name,
        tool_name=body.tool_name,
        condition_expr=body.condition_expr,
        approver_user_ids=body.approver_user_ids,
        notify_conversation_id=body.notify_conversation_id,
        auto_expire_minutes=body.auto_expire_minutes,
        post_exec_mode=body.post_exec_mode,
        enabled=body.enabled,
        priority=body.priority,
    )
    return _to_response(p)


@router.get("/{policy_id}", response_model=ApprovalPolicy)
async def get_policy(
    policy_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalPolicy:
    p = await _get_or_404(policy_id, tenant_id=user.tenant_id)
    return _to_response(p)


@router.patch("/{policy_id}", response_model=ApprovalPolicy)
async def patch_policy(
    policy_id: str,
    body: ApprovalPolicyUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalPolicy:
    _require_manage(user)
    await _get_or_404(policy_id, tenant_id=user.tenant_id)
    set_fields = body.model_fields_set
    kwargs: dict[str, object] = {}
    for field in (
        "mcp_server_name",
        "tool_name",
        "condition_expr",
        "approver_user_ids",
        "notify_conversation_id",
        "auto_expire_minutes",
        "post_exec_mode",
        "enabled",
        "priority",
    ):
        if field in set_fields:
            kwargs[field] = getattr(body, field)
    updated = await update_approval_policy(
        policy_id, tenant_id=user.tenant_id, **kwargs,  # type: ignore[arg-type]
    )
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Approval policy not found.",
            status_code=404,
            details={"policy_id": policy_id},
        )
    return _to_response(updated)


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Delete a policy.

    Pending ``approval_requests`` linked to this policy have their
    ``policy_id`` set to NULL via the FK cascade; they do **not**
    cascade-delete. This preserves the audit trail of in-flight
    decisions across a policy retraction.
    """
    _require_manage(user)
    await _get_or_404(policy_id, tenant_id=user.tenant_id)
    await delete_approval_policy(policy_id, tenant_id=user.tenant_id)
    return Response(status_code=204)
