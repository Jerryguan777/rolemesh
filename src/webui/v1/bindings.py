"""``/api/v1/coworkers/{id}/bindings`` REST surface (PR24).

Channel bindings tie a coworker to one channel-type connection
(Slack workspace, Telegram bot, in-browser web). The same surface
existed on the legacy ``/api/admin/agents/{id}/bindings`` path; this
module ports it to the v1 contract so the SPA can move off admin.

Wire vs. DB:
* `credentials` is write-only — `ChannelBinding` (the response shape)
  intentionally omits it. Sending tokens back on every list would
  let a leaked bearer + GET round-trip exfiltrate every workspace's
  bot_token; gate exposure to the moment the user is upserting.
* `(coworker_id, channel_type)` is UNIQUE in the DB; POST returns
  409 on collision so the frontend can prompt "this coworker is
  already bound to Slack — edit the existing binding?"
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.db import (
    create_channel_binding,
    delete_channel_binding,
    get_channel_binding,
    get_channel_bindings_for_coworker,
    get_coworker,
    update_channel_binding,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import (
    ChannelBinding,
    ChannelBindingCreate,
    ChannelBindingUpdate,
)
from webui.v1.errors import ErrorResponseException, raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser
    from rolemesh.core.types import ChannelBinding as ChannelBindingDataclass

router = APIRouter(prefix="/coworkers/{coworker_id}/bindings", tags=["Coworkers"])


def _to_response(b: ChannelBindingDataclass) -> ChannelBinding:
    return ChannelBinding(
        id=b.id,
        coworker_id=b.coworker_id,
        tenant_id=b.tenant_id,
        channel_type=b.channel_type,  # type: ignore[arg-type]
        bot_display_name=b.bot_display_name,
        status=b.status,
        created_at=b.created_at or None,
    )


async def _ensure_coworker(coworker_id: str, *, tenant_id: str) -> None:
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


async def _get_binding_for_coworker(
    binding_id: str, *, coworker_id: str, tenant_id: str,
) -> ChannelBindingDataclass:
    try:
        binding = await get_channel_binding(binding_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        binding = None
    # The path constrains the binding to a specific coworker; verify
    # the relation here so a cross-coworker probe (legal binding_id +
    # wrong coworker_id) doesn't leak existence — return 404, not 403.
    if binding is None or binding.coworker_id != coworker_id:
        raise_error_response(
            "NOT_FOUND",
            "Binding not found.",
            status_code=404,
            details={"binding_id": binding_id, "coworker_id": coworker_id},
        )
    return binding


@router.get("", response_model=list[ChannelBinding])
async def list_bindings_endpoint(
    coworker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ChannelBinding]:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    bindings = await get_channel_bindings_for_coworker(
        coworker_id, tenant_id=user.tenant_id,
    )
    return [_to_response(b) for b in bindings]


@router.post("", response_model=ChannelBinding, status_code=201)
async def create_binding_endpoint(
    coworker_id: str,
    body: ChannelBindingCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ChannelBinding:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    try:
        binding = await create_channel_binding(
            coworker_id=coworker_id,
            tenant_id=user.tenant_id,
            channel_type=body.channel_type,
            credentials=body.credentials or None,
            bot_display_name=body.bot_display_name,
        )
    except asyncpg.UniqueViolationError as exc:
        raise ErrorResponseException(
            status_code=409,
            code="RESOURCE_IN_USE",
            message=(
                f"Coworker already has a binding for channel_type "
                f"{body.channel_type!r}; PATCH the existing binding "
                "instead of creating a new one."
            ),
            details={
                "coworker_id": coworker_id,
                "channel_type": body.channel_type,
            },
        ) from exc
    return _to_response(binding)


@router.get("/{binding_id}", response_model=ChannelBinding)
async def get_binding_endpoint(
    coworker_id: str,
    binding_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ChannelBinding:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    binding = await _get_binding_for_coworker(
        binding_id, coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    return _to_response(binding)


@router.patch("/{binding_id}", response_model=ChannelBinding)
async def update_binding_endpoint(
    coworker_id: str,
    binding_id: str,
    body: ChannelBindingUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ChannelBinding:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    await _get_binding_for_coworker(
        binding_id, coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    # ``status`` is server-managed (gateway sets it on (re)connect);
    # the wire surface doesn't accept it on PATCH, so the DB helper
    # gets ``status=None`` explicitly to avoid clearing it.
    updated = await update_channel_binding(
        binding_id,
        tenant_id=user.tenant_id,
        credentials=body.credentials,
        bot_display_name=body.bot_display_name,
        status=None,
    )
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Binding not found.",
            status_code=404,
            details={"binding_id": binding_id},
        )
    return _to_response(updated)


@router.delete("/{binding_id}", status_code=204)
async def delete_binding_endpoint(
    coworker_id: str,
    binding_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    await _ensure_coworker(coworker_id, tenant_id=user.tenant_id)
    await _get_binding_for_coworker(
        binding_id, coworker_id=coworker_id, tenant_id=user.tenant_id,
    )
    await delete_channel_binding(binding_id, tenant_id=user.tenant_id)
    return Response(status_code=204)
