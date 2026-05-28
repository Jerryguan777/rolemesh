"""``/api/v1/me/channel-links/...`` — IM linking (v6.1 §P1.4).

The WebUI side of the Telegram link flow. Three handlers:

- ``POST /api/v1/me/channel-links/telegram`` mints a one-shot token,
  returns it plus an ``https://t.me/<bot>?start=<token>`` deep-link
  when a bot @username is known. The token is consumed by
  :func:`rolemesh.channels.telegram_gateway._handle_start_command`.
- ``GET /api/v1/me/channel-links/telegram`` lists the caller's
  active Telegram identities so the SPA can render the "connected"
  state and individual unbind buttons.
- ``DELETE /api/v1/me/channel-links/{identity_id}`` removes one
  identity row; admission for the underlying conversation flips
  back to "unlinked" on the next inbound message (Checkpoint 4).

Auth: ``get_current_user`` for all three — these are per-user
operations. Cross-user identity access is prevented at the DB layer
(identity_id + user_id filter).
"""

from __future__ import annotations

import uuid as _uuid

from fastapi import APIRouter, Depends, Response

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_link_token,
    delete_channel_identity,
    get_channel_bindings_for_tenant,
    list_channel_identities_for_user,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import ChannelLinkIdentity, ChannelLinkToken
from webui.v1.errors import raise_error_response

router = APIRouter(tags=["Auth"])


def _require_real_user(user: AuthenticatedUser) -> None:
    """Reject the single-token ``ADMIN_BOOTSTRAP_TOKEN`` actor.

    The bootstrap actor's ``user_id`` is the sentinel string
    ``"bootstrap"`` — not a UUID, so the IM identity tables (UUID FK)
    can't carry rows for it. The flow only makes sense for real,
    DB-backed users; surface that with a 422 + a hint instead of
    blowing up on the UUID cast.

    Bootstrap mode is supported by ``BOOTSTRAP_USERS`` (a JSON
    multi-user map whose user_ids are derived via uuid5 and persisted
    as real ``users`` rows) — that path works fine through here.
    """
    try:
        _uuid.UUID(user.user_id)
    except (ValueError, AttributeError):
        raise_error_response(
            "ACTOR_NOT_LINKABLE",
            "The bootstrap admin token has no real user record; "
            "linking IM channels requires logging in as a real user. "
            "Configure BOOTSTRAP_USERS or use OIDC.",
            status_code=422,
            details={"actor": user.user_id},
        )


def _build_deep_link(bot_username: str | None, token: str) -> str | None:
    """Synthesise the Telegram deep-link if a bot @handle is known.

    Returns ``None`` when no Telegram binding has reported its
    @username yet (a brand-new binding that has not yet started
    polling, or a tenant that has not provisioned any bot). The SPA
    falls back to showing the raw ``token`` for manual paste.
    """
    if not bot_username:
        return None
    # Telegram bot usernames are ASCII; no encoding required.
    return f"https://t.me/{bot_username}?start={token}"


@router.post(
    "/me/channel-links/telegram",
    response_model=ChannelLinkToken,
    status_code=201,
)
async def issue_telegram_link_token(
    user: AuthenticatedUser = Depends(get_current_user),
) -> ChannelLinkToken:
    """Mint a Telegram link token for the calling user.

    The tenant must have at least one ``channel_bindings`` row of
    ``channel_type='telegram'``; without one there is no bot for the
    user to send ``/start <token>`` to. We surface that as a 409
    so the SPA can render a "no Telegram bot configured for this
    tenant" hint instead of producing a dangling token.
    """
    _require_real_user(user)
    bindings = await get_channel_bindings_for_tenant(
        user.tenant_id, "telegram"
    )
    if not bindings:
        raise_error_response(
            "RESOURCE_NOT_AVAILABLE",
            "No Telegram bot is configured for this tenant.",
            status_code=409,
            details={"tenant_id": user.tenant_id},
        )
    # Prefer the oldest binding with a known @username — that's the
    # one most likely to still be live and reachable. Fall back to
    # any binding (deep_link will be None and the SPA shows the
    # raw token for paste).
    bot_username: str | None = None
    for b in bindings:
        if b.bot_username:
            bot_username = b.bot_username
            break

    token, expires_at = await create_link_token(
        user.user_id, user.tenant_id, "telegram"
    )
    return ChannelLinkToken(
        token=token,
        expires_at=expires_at.isoformat(),
        deep_link=_build_deep_link(bot_username, token),
    )


@router.get(
    "/me/channel-links/telegram",
    response_model=list[ChannelLinkIdentity],
)
async def list_telegram_links(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ChannelLinkIdentity]:
    """All currently-linked Telegram identities for the caller.

    The SPA polls this endpoint after issuing a token to detect when
    the user has completed ``/start <token>`` in Telegram. A list
    (not a single object) anticipates decision #13 — one user can
    bind multiple Telegram accounts.
    """
    _require_real_user(user)
    identities = await list_channel_identities_for_user(
        user.user_id, user.tenant_id
    )
    return [
        ChannelLinkIdentity(
            id=i.id,
            platform="telegram",
            channel_id=i.channel_id,
            created_at=i.created_at or None,
        )
        for i in identities
        if i.platform == "telegram"
    ]


@router.delete(
    "/me/channel-links/{identity_id}",
    status_code=204,
)
async def unlink_channel_identity(
    identity_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Unbind one identity row.

    Returns 404 when the row is missing or owned by another user —
    the (user_id, tenant_id) filter in the DB DELETE guarantees the
    handler cannot leak the existence of another user's link via a
    different status code.
    """
    _require_real_user(user)
    deleted = await delete_channel_identity(
        identity_id, user.user_id, user.tenant_id
    )
    if not deleted:
        raise_error_response(
            "NOT_FOUND",
            "Channel link not found.",
            status_code=404,
            details={"identity_id": identity_id},
        )
    return Response(status_code=204)
