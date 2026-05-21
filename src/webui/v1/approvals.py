"""``/api/v1/approvals`` REST surface (design §3 Phase 3).

Four endpoints:

* ``GET    /api/v1/approvals`` — list pending / scoped to me by
  default, ``scope=all`` for admins.
* ``GET    /api/v1/approvals/{id}`` — request + inline audit log.
* ``GET    /api/v1/approvals/{id}/audit-log`` — audit only.
* ``POST   /api/v1/approvals/{id}/decide`` — engine handoff.

All mutating paths flow through the **same** engine instance the
legacy ``/api/admin/approvals/{id}/decide`` endpoint uses — see
:mod:`webui.v1.approval_engine_registry`. Both routes share the
state machine without duplicating implementation (the 03a session
prompt's "avoid double impl" constraint).

INV-7 wire translation (``http_action_to_outcome``) and INV-4
audit actor resolution (``resolve_actor_user_id``) happen at the
handler boundary; the engine itself sees only the closed enum
and a real UUID.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends

from rolemesh.approval.engine import ConflictError, ForbiddenError
from rolemesh.approval.enum_translate import http_action_to_outcome
from rolemesh.auth.bootstrap_actor import resolve_actor_user_id
from rolemesh.auth.permissions import user_can
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    get_approval_request,
    list_approval_audit,
    list_approval_requests,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import (
    ApprovalAuditEntry,
    ApprovalDecide,
    ApprovalListScope,
    ApprovalRequest,
    ApprovalRequestDetail,
)
from webui.v1.approval_engine_registry import get_approval_engine
from webui.v1.errors import ErrorResponseException, raise_error_response

router = APIRouter(prefix="/approvals", tags=["Approvals"])


# ---------------------------------------------------------------------------
# Note sanitisation — same shape as the legacy admin endpoint so the two
# surfaces store identical text given identical input.
# ---------------------------------------------------------------------------


def _sanitize_note(note: str | None) -> str | None:
    """Trim and strip ASCII/C1 control characters.

    Pydantic already caps at 1000 chars; this also drops control
    sequences that a downstream Markdown-rendering channel could
    interpret (`\\r`, `\\x1b`). Keeping the filter at the REST
    boundary means stored notes are clean without a per-channel
    sanitiser further down the pipe.
    """
    if note is None:
        return None
    cleaned = "".join(
        c
        for c in note
        if c == "\n" or c == "\t" or (0x20 <= ord(c) < 0x7F) or ord(c) > 0xA0
    ).strip()
    return cleaned or None


# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------


def _request_to_response(r: object) -> ApprovalRequest:
    return ApprovalRequest(
        id=r.id,
        tenant_id=r.tenant_id,
        coworker_id=r.coworker_id,
        conversation_id=r.conversation_id,
        policy_id=r.policy_id,
        user_id=r.user_id,
        job_id=r.job_id,
        mcp_server_name=r.mcp_server_name,
        actions=list(r.actions),
        action_hashes=list(r.action_hashes),
        rationale=r.rationale,
        source=r.source,  # type: ignore[arg-type]
        status=r.status,  # type: ignore[arg-type]
        post_exec_mode=r.post_exec_mode,  # type: ignore[arg-type]
        resolved_approvers=list(r.resolved_approvers),
        requested_at=r.requested_at,
        expires_at=r.expires_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _audit_to_response(e: object) -> ApprovalAuditEntry:
    return ApprovalAuditEntry(
        id=e.id,
        request_id=e.request_id,
        action=e.action,
        actor_user_id=e.actor_user_id,
        note=e.note,
        metadata=dict(e.metadata),
        created_at=e.created_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _get_request_or_404(
    request_id: str, *, tenant_id: str
) -> object:
    try:
        req = await get_approval_request(request_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        req = None
    if req is None:
        raise_error_response(
            "NOT_FOUND",
            "Approval request not found.",
            status_code=404,
            details={"request_id": request_id},
        )
    return req


@router.get("", response_model=list[ApprovalRequest])
async def list_approvals(
    status: str | None = None,
    scope: ApprovalListScope = "mine",
    coworker_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalRequest]:
    """List approval requests.

    Default behaviour (``scope=mine``) returns rows where the
    caller is in ``resolved_approvers``. ``scope=all`` is admin-
    only and returns the full tenant view — useful for the
    operator-facing dashboards but a privacy leak for non-admins
    (the requests carry user-supplied free-text rationale + tool
    arguments).
    """
    if scope == "all" and not user_can(user.role, "view_all_conversations"):
        raise_error_response(
            "FORBIDDEN",
            "scope=all requires admin+ role.",
            status_code=403,
            details={"required": "view_all_conversations"},
        )
    rows = await list_approval_requests(
        user.tenant_id, status=status, coworker_id=coworker_id
    )
    if scope == "mine":
        rows = [r for r in rows if user.user_id in r.resolved_approvers]
    return [_request_to_response(r) for r in rows]


@router.get("/{request_id}", response_model=ApprovalRequestDetail)
async def get_approval(
    request_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalRequestDetail:
    req = await _get_request_or_404(request_id, tenant_id=user.tenant_id)
    audit = await list_approval_audit(request_id, tenant_id=user.tenant_id)
    base = _request_to_response(req).model_dump()
    return ApprovalRequestDetail(
        **base,
        audit_log=[_audit_to_response(e) for e in audit],
    )


@router.get(
    "/{request_id}/audit-log",
    response_model=list[ApprovalAuditEntry],
)
async def get_audit_log(
    request_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalAuditEntry]:
    await _get_request_or_404(request_id, tenant_id=user.tenant_id)
    rows = await list_approval_audit(request_id, tenant_id=user.tenant_id)
    return [_audit_to_response(r) for r in rows]


@router.post("/{request_id}/decide", response_model=ApprovalRequest)
async def decide(
    request_id: str,
    body: ApprovalDecide,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalRequest:
    """Approve or reject a pending request.

    Boundary work:

    1. INV-7: translate the HTTP wire enum to the engine outcome.
       Pydantic already gates ``action`` to ``approve|reject``;
       :func:`http_action_to_outcome` raises ``ValueError`` if
       anything else slips through (would only happen if Pydantic
       and the translator drift), which we surface as 422 with the
       ``INVALID_DECISION_ACTION`` envelope code.
    2. INV-4: resolve the audit actor through
       :func:`resolve_actor_user_id`. Single-token bootstrap mode
       falls back to the tenant owner; multi-user bootstrap mode
       passes the real UUID through.
    3. Engine handoff: same ``handle_decision`` call the legacy
       admin endpoint makes — only one implementation of the
       state machine.
    """
    engine = get_approval_engine()
    if engine is None:
        raise_error_response(
            "APPROVAL_ENGINE_UNAVAILABLE",
            "Approval engine is not wired into this process.",
            status_code=503,
        )
    # 404 before engine work so a forged or cross-tenant request_id
    # returns the same shape as a missing row, without leaking
    # decision-machine timing.
    await _get_request_or_404(request_id, tenant_id=user.tenant_id)
    try:
        outcome = http_action_to_outcome(body.action)
    except ValueError as exc:
        raise_error_response(
            "INVALID_DECISION_ACTION",
            str(exc),
            status_code=422,
            details={"action": body.action},
        )
    actor = await resolve_actor_user_id(user.tenant_id, user.user_id)
    try:
        updated = await engine.handle_decision(
            request_id=request_id,
            tenant_id=user.tenant_id,
            outcome=outcome,
            user_id=actor,
            note=_sanitize_note(body.note),
        )
    except ForbiddenError as exc:
        raise ErrorResponseException(
            status_code=403,
            code="FORBIDDEN",
            message="User is not an authorised approver.",
        ) from exc
    except ConflictError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="ALREADY_DECIDED",
            message=f"Request already {exc.current_status}.",
            details={"current_status": exc.current_status},
        ) from exc
    except LookupError as exc:
        # decide_approval_request_full → "missing" (CTE found
        # nothing). This usually means a concurrent DELETE landed
        # between the 404 probe and the engine call.
        raise ErrorResponseException(
            status_code=404,
            code="NOT_FOUND",
            message="Approval request not found.",
            details={"request_id": request_id},
        ) from exc
    return _request_to_response(updated)
