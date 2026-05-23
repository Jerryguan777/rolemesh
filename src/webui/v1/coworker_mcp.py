"""``/api/v1/coworkers/{id}/mcp-servers`` relation layer.

Manages the ``coworker_mcp_servers`` junction. The MCP server CRUD
itself lives in :mod:`webui.v1.mcp_servers`; this module only deals
with the (coworker, mcp_server) binding plus the ``enabled_tools``
tri-state.

Every mutating handler publishes one ``web.coworker.mcp_changed``
event so the orchestrator can refresh the coworker's MCP projection
without a full restart (design §7 hot-load matrix). The
orchestrator-side subscriber lives in
``rolemesh.orchestration.coworker_hot_reload.subscribe_coworker_mcp_changed``.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    CoworkerMCPBinding,
    bind_coworker_mcp_server,
    get_coworker,
    get_mcp_server,
    list_coworker_mcp_bindings,
    set_coworker_mcp_enabled_tools,
    unbind_coworker_mcp_server,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import (
    CoworkerMCPBindingCreate,
    CoworkerMCPBindingResponse,
    CoworkerMCPBindingUpdate,
)
from webui.v1 import coworker_events
from webui.v1.errors import ErrorResponseException, raise_error_response

router = APIRouter(
    prefix="/coworkers/{coworker_id}/mcp-servers",
    tags=["Coworkers"],
)


def _binding_to_response(b: CoworkerMCPBinding) -> CoworkerMCPBindingResponse:
    return CoworkerMCPBindingResponse(
        coworker_id=b.coworker_id,
        mcp_server_id=b.mcp_server_id,
        enabled_tools=b.enabled_tools,
    )


async def _ensure_coworker(coworker_id: str, *, tenant_id: str) -> None:
    """Raise the v1 404 envelope when the parent coworker is missing."""
    try:
        cw = await get_coworker(coworker_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        cw = None
    if cw is None:
        raise_error_response(
            "NOT_FOUND",
            "Coworker not found.",
            status_code=404,
            details={"coworker_id": coworker_id},
        )


async def _ensure_mcp_server(mcp_id: str, *, tenant_id: str) -> None:
    try:
        row = await get_mcp_server(mcp_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "MCP server not found.",
            status_code=404,
            details={"mcp_server_id": mcp_id},
        )


@router.get("", response_model=list[CoworkerMCPBindingResponse])
async def list_bindings_endpoint(
    coworker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[CoworkerMCPBindingResponse]:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    rows = await list_coworker_mcp_bindings(
        coworker_id, tenant_id=user.tenant_id,
    )
    return [_binding_to_response(r) for r in rows]


@router.post(
    "", response_model=CoworkerMCPBindingResponse, status_code=201,
)
async def bind_endpoint(
    coworker_id: str,
    body: CoworkerMCPBindingCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CoworkerMCPBindingResponse:
    """Bind ``mcp_server_id`` to this coworker.

    Validates both parents (coworker + MCP server) belong to the
    caller's tenant before INSERT; the junction's RLS policy
    enforces this transitively but the explicit 404 is friendlier
    than letting the INSERT silently no-op on a foreign-tenant row.
    """
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    await _ensure_mcp_server(body.mcp_server_id, tenant_id=user.tenant_id)
    try:
        binding = await bind_coworker_mcp_server(
            coworker_id=coworker_id,
            mcp_server_id=body.mcp_server_id,
            enabled_tools=body.enabled_tools,
            tenant_id=user.tenant_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message="MCP server already bound to this coworker.",
            details={
                "coworker_id": coworker_id,
                "mcp_server_id": body.mcp_server_id,
            },
        ) from exc
    await coworker_events.publish_coworker_mcp_changed(
        coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    return _binding_to_response(binding)


@router.patch(
    "/{mcp_id}", response_model=CoworkerMCPBindingResponse,
)
async def patch_binding_endpoint(
    coworker_id: str,
    mcp_id: str,
    body: CoworkerMCPBindingUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CoworkerMCPBindingResponse:
    """Change ``enabled_tools`` on an existing binding.

    The PATCH semantics here are deliberately narrower than the rest
    of the v1 surface: there is exactly one mutable column, and
    omitting ``enabled_tools`` from the body is treated as "no
    change" (returns the current row) rather than "set to null".
    The "set to null" case is exposed via an explicit
    ``{"enabled_tools": null}``.
    """
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    if "enabled_tools" not in body.model_fields_set:
        # No-op: return the current state.
        rows = await list_coworker_mcp_bindings(
            coworker_id, tenant_id=user.tenant_id,
        )
        for b in rows:
            if b.mcp_server_id == mcp_id:
                return _binding_to_response(b)
        raise_error_response(
            "NOT_FOUND",
            "Binding not found.",
            status_code=404,
            details={"coworker_id": coworker_id, "mcp_server_id": mcp_id},
        )
    updated = await set_coworker_mcp_enabled_tools(
        coworker_id=coworker_id,
        mcp_server_id=mcp_id,
        enabled_tools=body.enabled_tools,
        tenant_id=user.tenant_id,
    )
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Binding not found.",
            status_code=404,
            details={"coworker_id": coworker_id, "mcp_server_id": mcp_id},
        )
    await coworker_events.publish_coworker_mcp_changed(
        coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    return _binding_to_response(updated)


@router.delete("/{mcp_id}", status_code=204)
async def unbind_endpoint(
    coworker_id: str,
    mcp_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    removed = await unbind_coworker_mcp_server(
        coworker_id=coworker_id,
        mcp_server_id=mcp_id,
        tenant_id=user.tenant_id,
    )
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "Binding not found.",
            status_code=404,
            details={"coworker_id": coworker_id, "mcp_server_id": mcp_id},
        )
    await coworker_events.publish_coworker_mcp_changed(
        coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    return Response(status_code=204)
