"""``/api/v1/auth/*`` and ``/api/v1/me`` endpoints (design §3 / §4).

01b will wire the WebSocket handshake on top of the ticket this
module mints. Splitting the issuer (here) from the verifier
(:mod:`rolemesh.auth.ws_ticket`) lets either side change shape
without dragging the other along — the only thing they share is
the payload contract.

The handler suite covers three concerns:

* **AuthConfig** — boot-time hint the SPA reads before sending the
  first authenticated request. Today it reports ``mode="bootstrap"``
  whenever ``BOOTSTRAP_USERS`` is configured (live-smoke posture)
  and the configured AuthProvider mode otherwise.
* **WsTicket** — short-lived bound JWT. Conversation membership
  is verified server-side at issue time, so the WS handshake can
  trust the ticket payload without an extra DB hop.
* **Me** — current user surface for the SPA user menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.auth.permissions import role_capabilities
from rolemesh.auth.ws_ticket import WsTicketError, issue_ws_ticket
from rolemesh.db import get_conversation
from webui.dependencies import get_current_user, user_can_access_conversation
from webui.schemas_v1 import (
    AuthConfig,
    Me,
    WsTicket,
    WsTicketRequest,
)
from webui.v1.errors import ErrorResponseException, raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

router = APIRouter(prefix="/auth", tags=["Auth"])


def _detect_auth_mode() -> tuple[str, str | None]:
    """Return ``(mode, login_url)`` for ``/api/v1/auth/config``.

    Live smoke runs everything through the bootstrap fast-path, so
    when ``BOOTSTRAP_USERS`` is set we advertise that to the SPA —
    its UI then knows to use the token it was handed instead of
    redirecting through an IdP. Production with OIDC sets
    ``AUTH_MODE=oidc`` and the SPA picks up the login URL.

    The hint is *informational*: the actual rejection happens in
    :mod:`webui.auth.authenticate_ws`. A wrong mode here only
    misleads the UI, never leaks access.
    """
    import os

    if os.environ.get("BOOTSTRAP_USERS"):
        return "bootstrap", None
    mode = os.environ.get("AUTH_MODE", "external")
    if mode == "oidc":
        login = os.environ.get("OIDC_REDIRECT_URI") or None
        return "oidc", login
    if mode == "builtin":
        return "builtin", None
    return "external", None


@router.get("/config", response_model=AuthConfig)
async def get_auth_config() -> AuthConfig:
    """Public auth-mode hint. No auth required by design (§3).

    The SPA boots before it has a session, so this endpoint is
    deliberately open. It returns enough for the SPA to choose
    *how* to authenticate (bootstrap token vs OIDC redirect) but
    never token *values*.
    """
    mode, login_url = _detect_auth_mode()
    return AuthConfig(mode=mode, login_url=login_url)  # type: ignore[arg-type]


@router.post("/ws-ticket", response_model=WsTicket)
async def post_ws_ticket(
    body: WsTicketRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WsTicket:
    """Issue a short-lived ticket bound to ``conversation_id``.

    The conversation membership check is *not* optional: a ticket
    that an attacker could mint for an arbitrary
    ``conversation_id`` they don't actually own would let them
    attach a WS to any conversation in the tenant. The handler
    verifies the conversation exists in the caller's tenant
    before signing — RLS plus the explicit ``tenant_id`` predicate
    in :func:`rolemesh.db.get_conversation` enforces the boundary.

    Even an authenticated cross-tenant guess fails because
    ``get_conversation`` returns ``None`` when the tenant_id
    doesn't match — we collapse that to 404 NOT_FOUND rather than
    403 so the existence isn't leaked.

    Within the tenant the per-user ownership rule applies with the
    same weight: the WS handshake trusts this ticket without
    re-checking, so minting is where "may this user attach to this
    conversation" is decided. Without it a member could stream — and
    send into — another member's conversation that every REST path
    correctly 404s. Not-owned collapses to the same 404.
    """
    try:
        conv = await get_conversation(body.conversation_id, tenant_id=user.tenant_id)
    except asyncpg.DataError:
        conv = None
    if conv is None or not user_can_access_conversation(
        conversation=conv, user=user
    ):
        raise_error_response(
            "NOT_FOUND",
            "Conversation not found.",
            status_code=404,
            details={"conversation_id": body.conversation_id},
        )

    try:
        token, exp = issue_ws_ticket(
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            conversation_id=body.conversation_id,
        )
    except WsTicketError as exc:
        # The signing secret is missing — surface to the caller as
        # 500-ish with a clear code so an operator can fix the env.
        raise ErrorResponseException(
            status_code=500,
            code=exc.code,
            message=str(exc),
        ) from exc
    return WsTicket(ticket=token, expires_in_s=exp)


me_router = APIRouter(tags=["Auth"])


@me_router.get("/me", response_model=Me)
async def get_me(
    user: AuthenticatedUser = Depends(get_current_user),
) -> Me:
    """Identity of the calling user — what the SPA shows in the menu."""
    return _user_to_me(user)


def _user_to_me(user: AuthenticatedUser) -> Me:
    return Me(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        name=user.name,
        email=user.email,
        role=user.role,  # type: ignore[arg-type]
        plane="platform" if user.role == "platform_admin" else "tenant",
        # Single source of truth: the role->action matrix. The SPA renders
        # from this list; the server still enforces via require_action.
        capabilities=role_capabilities(user.role),  # type: ignore[arg-type]
    )


# Pyright shim: silence unused-import warning for Response without
# adding a runtime cost.
_ = Response
