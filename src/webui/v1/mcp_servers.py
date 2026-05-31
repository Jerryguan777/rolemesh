"""``/api/v1/mcp-servers`` REST surface (design §3 Phase 2).

Full CRUD against the ``mcp_servers`` table with RLS + explicit
``WHERE tenant_id`` belt-and-braces (INV-1). Every mutating handler
emits exactly one ``egress.mcp.changed`` event so the gateway picks
up routing deltas without an orchestrator restart.

User-mode credential injection (``auth_mode=user``) is wired
end-to-end downstream in 02c; this surface accepts the literal but
makes no assumption about the injection path.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    MCPServerRow,
    create_mcp_server,
    delete_mcp_server,
    get_mcp_server,
    get_mcp_server_references,
    list_mcp_servers,
    update_mcp_server,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import MCPServer, MCPServerCreate, MCPServerUpdate
from webui.v1 import mcp_events
from webui.v1.errors import ErrorResponseException, raise_error_response

router = APIRouter(prefix="/mcp-servers", tags=["MCPServers"])


def _row_to_response(row: MCPServerRow) -> MCPServer:
    return MCPServer(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        type=row.type,  # type: ignore[arg-type]
        url=row.url,
        auth_mode=row.auth_mode,  # type: ignore[arg-type]
        extra_headers=dict(row.extra_headers or {}),
        tool_reversibility={
            str(k): bool(v) for k, v in (row.tool_reversibility or {}).items()
        },
        description=row.description,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


async def _get_or_404(mcp_id: str, *, tenant_id: str) -> MCPServerRow:
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
    return row


@router.get("", response_model=list[MCPServer])
async def list_endpoint(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[MCPServer]:
    rows = await list_mcp_servers(user.tenant_id)
    return [_row_to_response(r) for r in rows]


@router.post("", response_model=MCPServer, status_code=201)
async def create_endpoint(
    body: MCPServerCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServer:
    try:
        row = await create_mcp_server(
            tenant_id=user.tenant_id,
            name=body.name,
            type=body.type,
            url=body.url,
            auth_mode=body.auth_mode,
            extra_headers=body.extra_headers,
            tool_reversibility=body.tool_reversibility,
            description=body.description,
        )
    except asyncpg.UniqueViolationError as exc:
        # UNIQUE (tenant_id, name) — surface as 409 with the offending
        # constraint name so a client can decide which field to flag.
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message="An MCP server with this name already exists in the tenant.",
            details={"constraint": getattr(exc, "constraint_name", "") or ""},
        ) from exc
    await mcp_events.publish_mcp_server_changed(action="created", row=row)
    return _row_to_response(row)


@router.get("/{mcp_id}", response_model=MCPServer)
async def get_endpoint(
    mcp_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServer:
    row = await _get_or_404(mcp_id, tenant_id=user.tenant_id)
    return _row_to_response(row)


@router.patch("/{mcp_id}", response_model=MCPServer)
async def patch_endpoint(
    mcp_id: str,
    body: MCPServerUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServer:
    """Partial update; ``None`` clears nullable columns, absent leaves alone.

    Pydantic's ``model_fields_set`` is the discriminator — fields the
    caller did not include are not forwarded to the DB helper, while
    explicit ``None`` is forwarded (and the column is nullable in
    the schema, so the DELETE-the-credential reference is supported).
    """
    await _get_or_404(mcp_id, tenant_id=user.tenant_id)
    set_fields = body.model_fields_set
    kwargs: dict[str, object] = {}
    for field in (
        "name",
        "type",
        "url",
        "auth_mode",
        "extra_headers",
        "tool_reversibility",
        "description",
    ):
        if field in set_fields:
            kwargs[field] = getattr(body, field)
    try:
        updated = await update_mcp_server(
            mcp_id, tenant_id=user.tenant_id, **kwargs,  # type: ignore[arg-type]
        )
    except asyncpg.UniqueViolationError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message="An MCP server with this name already exists in the tenant.",
            details={"constraint": getattr(exc, "constraint_name", "") or ""},
        ) from exc
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "MCP server not found.",
            status_code=404,
            details={"mcp_server_id": mcp_id},
        )
    await mcp_events.publish_mcp_server_changed(action="updated", row=updated)
    return _row_to_response(updated)


@router.delete("/{mcp_id}", status_code=204)
async def delete_endpoint(
    mcp_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Delete an MCP server.

    Returns 409 ``RESOURCE_IN_USE`` (with ``details.coworker_ids``)
    when at least one coworker still binds this server. Reference
    check before the DELETE so a concurrent bind that arrives after
    this read still loses to the 409 path on the next attempt.
    """
    row = await _get_or_404(mcp_id, tenant_id=user.tenant_id)
    referencing = await get_mcp_server_references(
        mcp_id, tenant_id=user.tenant_id,
    )
    if referencing:
        raise_error_response(
            "RESOURCE_IN_USE",
            (
                f"MCP server is in use by {len(referencing)} "
                f"coworker(s); unbind them before deleting."
            ),
            status_code=409,
            details={"coworker_ids": referencing, "mcp_server_id": mcp_id},
        )
    removed = await delete_mcp_server(mcp_id, tenant_id=user.tenant_id)
    if not removed:
        raise_error_response(
            "NOT_FOUND",
            "MCP server not found.",
            status_code=404,
            details={"mcp_server_id": mcp_id},
        )
    await mcp_events.publish_mcp_server_deleted(name=row.name)
    return Response(status_code=204)
