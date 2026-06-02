"""``/api/v1/approval-policies`` + ``/api/v1/approval-requests`` REST surface.

The HITL tool-approval policy CRUD (docs/21-hitl-approval-plan.md §10 S5) plus a
small read of in-flight pending requests for web-reconnect re-render (§S4 notes
follow-up: the ``approval_requests`` row is the authoritative source for a card,
so a browser that dropped its socket re-renders from here).

Two routers ship from one module because they are the same feature's two
surfaces. Both are strictly tenant-scoped: every DB helper goes through
``tenant_conn`` (RLS-bound) **and** carries an explicit ``WHERE tenant_id``
(INV-1 belt-and-braces), so a guessed UUID from another tenant collapses to the
same 404 a non-existent id gets — no cross-tenant existence oracle.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Query, Response

from agent_runner.approval.policy import ApprovalPolicy as ApprovalPolicyValue
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db.approval import (
    ApprovalRequest as ApprovalRequestRow,
)
from rolemesh.db.approval import (
    create_approval_policy,
    delete_approval_policy,
    get_approval_policy,
    list_approval_policies,
    list_pending_requests_for_tenant,
    update_approval_policy,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import (
    ApprovalPolicy,
    ApprovalPolicyCreate,
    ApprovalPolicyUpdate,
    ApprovalRequest,
)
from webui.v1.errors import raise_error_response

policies_router = APIRouter(prefix="/approval-policies", tags=["ApprovalPolicies"])
requests_router = APIRouter(prefix="/approval-requests", tags=["ApprovalPolicies"])


def _policy_to_response(p: ApprovalPolicyValue) -> ApprovalPolicy:
    return ApprovalPolicy(
        id=p.id,
        tenant_id=p.tenant_id,
        mcp_server_name=p.mcp_server_name,
        tool_name=p.tool_name,
        condition_expr=p.condition_expr,
        enabled=p.enabled,
        priority=p.priority,
        created_at=p.created_at.isoformat() if p.created_at else "",
        updated_at=p.updated_at.isoformat() if p.updated_at else "",
    )


def _request_to_response(r: ApprovalRequestRow) -> ApprovalRequest:
    # ``action`` is the {tool_name, params} snapshot. The decision UX (§1.2)
    # needs the raw params to be informative — the user cannot meaningfully
    # approve a tool call they can't see the arguments of. The endpoint is
    # already strictly tenant-scoped, so the params never cross a tenant edge.
    action = r.action if isinstance(r.action, dict) else {}
    tool_name = str(action.get("tool_name", ""))
    raw_params = action.get("params")
    params = raw_params if isinstance(raw_params, dict) else None
    return ApprovalRequest(
        request_id=r.id,
        conversation_id=r.conversation_id,
        mcp_server_name=r.mcp_server_name,
        tool_name=tool_name,
        action_summary=r.action_summary,
        requested_at=r.requested_at.isoformat() if r.requested_at else "",
        expires_at=r.expires_at.isoformat() if r.expires_at else "",
        params=params,
        coworker_id=r.coworker_id,
        rationale=r.rationale,
        status=r.status,
        decided_at=r.decided_at.isoformat() if r.decided_at else None,
        note=r.note,
    )


async def _get_policy_or_404(policy_id: str, *, tenant_id: str) -> ApprovalPolicyValue:
    try:
        policy = await get_approval_policy(policy_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        # A structurally-invalid UUID must surface the *same* 404 a valid-but-
        # absent id gets, so the route never leaks "is this a real uuid shape".
        policy = None
    if policy is None:
        raise_error_response(
            "NOT_FOUND",
            "Approval policy not found.",
            status_code=404,
            details={"policy_id": policy_id},
        )
    return policy


# ---------------------------------------------------------------------------
# approval_policies CRUD
# ---------------------------------------------------------------------------


@policies_router.get("", response_model=list[ApprovalPolicy])
async def list_policies_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalPolicy]:
    """List the tenant's approval policies (priority desc, then newest)."""
    policies = await list_approval_policies(user.tenant_id)
    return [_policy_to_response(p) for p in policies]


@policies_router.post("", response_model=ApprovalPolicy, status_code=201)
async def create_policy_endpoint(
    body: ApprovalPolicyCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalPolicy:
    """Create a policy. ``condition_expr`` is validated at the schema layer."""
    policy = await create_approval_policy(
        tenant_id=user.tenant_id,
        mcp_server_name=body.mcp_server_name,
        tool_name=body.tool_name,
        condition_expr=body.condition_expr,
        enabled=body.enabled,
        priority=body.priority,
    )
    return _policy_to_response(policy)


@policies_router.get("/{policy_id}", response_model=ApprovalPolicy)
async def get_policy_endpoint(
    policy_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalPolicy:
    policy = await _get_policy_or_404(policy_id, tenant_id=user.tenant_id)
    return _policy_to_response(policy)


@policies_router.patch("/{policy_id}", response_model=ApprovalPolicy)
async def patch_policy_endpoint(
    policy_id: str,
    body: ApprovalPolicyUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalPolicy:
    """Partial update; absent fields are left alone (``model_fields_set``).

    The 404 pre-check is scoped to the caller's tenant, so a cross-tenant id
    is refused *before* the UPDATE — it never even reaches the (also
    tenant-scoped) DML.
    """
    await _get_policy_or_404(policy_id, tenant_id=user.tenant_id)
    set_fields = body.model_fields_set
    kwargs: dict[str, object] = {}
    for field in (
        "mcp_server_name",
        "tool_name",
        "condition_expr",
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
    return _policy_to_response(updated)


@policies_router.delete("/{policy_id}", status_code=204)
async def delete_policy_endpoint(
    policy_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Delete a policy. 404 if it does not exist in the caller's tenant."""
    await _get_policy_or_404(policy_id, tenant_id=user.tenant_id)
    removed = await delete_approval_policy(policy_id, tenant_id=user.tenant_id)
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "Approval policy not found.",
            status_code=404,
            details={"policy_id": policy_id},
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# approval_requests — pending read (web reconnect re-render)
# ---------------------------------------------------------------------------


@requests_router.get("", response_model=list[ApprovalRequest])
async def list_pending_requests_endpoint(
    conversation_id: str | None = Query(
        default=None,
        description=(
            "Optional filter: only pending requests for this conversation. "
            "The SPA passes its current conversation on reconnect so it "
            "re-renders just that conversation's in-flight cards."
        ),
    ),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalRequest]:
    """Pending approval requests for the caller's tenant (oldest first).

    Authoritative source for re-rendering ✅/❌ cards after a socket drop. The
    optional ``conversation_id`` filter is applied in-process after the
    tenant-scoped read — both the read and the filter stay inside the tenant
    boundary, so it can never surface another tenant's request even if a
    foreign ``conversation_id`` is supplied.
    """
    pending = await list_pending_requests_for_tenant(user.tenant_id)
    if conversation_id is not None:
        pending = [r for r in pending if r.conversation_id == conversation_id]
    return [_request_to_response(r) for r in pending]
