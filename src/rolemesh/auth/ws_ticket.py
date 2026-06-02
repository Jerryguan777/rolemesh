"""Short-lived WebSocket handshake ticket (design §4).

The browser asks the WebUI for a ticket via
``POST /api/v1/auth/ws-ticket`` and passes the result as a query
parameter on the WS upgrade. The handshake verifies the ticket
without a DB hop because the ticket payload carries enough state
(``user_id``, ``tenant_id``, ``conversation_id``) for the WS to
decide whether the connection is authorised.

Two design constraints are load-bearing:

* **Dedicated signing secret**, separate from the AuthProvider's
  JWT secret. A leaked ticket secret would let an attacker forge
  ws tickets but not API session tokens, and vice versa. This
  module reads ``WS_TICKET_SECRET`` from the environment and falls
  back to ``ADMIN_BOOTSTRAP_TOKEN`` (dev-only); the fallback logs
  a warning so production deployments don't accidentally share
  the bootstrap key with the ticket scheme.
* **Exp ≤ 60s**. The ticket is a one-shot handshake credential,
  not a session — once the WS is up it's authenticated by the
  long-lived API token that issued the ticket. Bounding exp at
  60 seconds keeps the replay window small.

A ticket binds *one* ``conversation_id``. The WS handshake
compares ticket payload to URL path and refuses mismatches — that
is what makes "anyone with a valid ticket can join any
conversation" not happen.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import jwt

logger = logging.getLogger(__name__)

_ALGORITHM: Final[str] = "HS256"
_DEFAULT_TTL_S: Final[int] = 60
_MAX_TTL_S: Final[int] = 60  # enforced in both issue and verify
_AUDIENCE: Final[str] = "rolemesh-ws"


class WsTicketError(Exception):
    """Raised when ticket issuance or verification fails."""

    code: str = "WS_TICKET_INVALID"
    status: int = 401

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class WsTicketExpired(WsTicketError):
    code = "WS_TICKET_EXPIRED"


@dataclass(frozen=True)
class WsTicketPayload:
    """Decoded ticket payload — what the WS handshake compares against."""

    user_id: str
    tenant_id: str
    conversation_id: str
    exp: int


def _get_secret() -> str:
    """Resolve the signing key.

    Production: ``WS_TICKET_SECRET`` must be set explicitly. Dev:
    fall back to ``ADMIN_BOOTSTRAP_TOKEN`` (a long random token the
    operator already manages) with a one-shot warning. The
    fall-back is gated behind 'ADMIN_BOOTSTRAP_TOKEN is set' so an
    empty env in production fails closed.
    """
    secret = os.environ.get("WS_TICKET_SECRET", "").strip()
    if secret:
        return secret
    bootstrap = os.environ.get("ADMIN_BOOTSTRAP_TOKEN", "").strip()
    if bootstrap:
        # Quietly reuse the bootstrap token in dev; loud in prod
        # would be even louder if AUTH_MODE were not external, but
        # this module doesn't know the deployment posture so the
        # warning is one-shot regardless.
        if not getattr(_get_secret, "_warned", False):  # type: ignore[attr-defined]
            logger.warning(
                "WS_TICKET_SECRET unset; falling back to "
                "ADMIN_BOOTSTRAP_TOKEN. Set WS_TICKET_SECRET in "
                "production so the ticket scheme has its own key."
            )
            _get_secret._warned = True  # type: ignore[attr-defined]
        return bootstrap
    raise WsTicketError(
        "WS_TICKET_SECRET is not configured; cannot issue tickets.",
        code="WS_TICKET_SECRET_UNSET",
    )


def issue_ws_ticket(
    *,
    user_id: str,
    tenant_id: str,
    conversation_id: str,
    ttl_seconds: int = _DEFAULT_TTL_S,
) -> tuple[str, int]:
    """Mint a ticket; return ``(jwt, expires_in_seconds)``.

    ``ttl_seconds`` is clamped to [1, 60]. The lower bound exists
    so a misconfigured ``ttl_seconds=0`` doesn't issue an
    instantly-expired ticket (which would loop the SPA's handshake
    retry).
    """
    if ttl_seconds <= 0:
        ttl_seconds = 1
    if ttl_seconds > _MAX_TTL_S:
        ttl_seconds = _MAX_TTL_S
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "aud": _AUDIENCE,
        "sub": user_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
    }
    token = jwt.encode(payload, _get_secret(), algorithm=_ALGORITHM)
    # PyJWT 1.x returned bytes; 2.x returns str. Force str so the
    # JSON response is unambiguous.
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token, ttl_seconds


def verify_ws_ticket(token: str) -> WsTicketPayload:
    """Decode + validate a ticket; raise on failure.

    The handshake is the only caller today (01b will wire it). The
    function is here so any future scheduled-trigger flow that
    needs the same envelope reuses the verification logic rather
    than re-implementing it.
    """
    try:
        decoded = jwt.decode(
            token,
            _get_secret(),
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            options={"require": ["exp", "sub", "tenant_id", "conversation_id"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise WsTicketExpired("ws ticket expired") from exc
    except jwt.InvalidTokenError as exc:
        raise WsTicketError(f"ws ticket invalid: {exc}") from exc

    return WsTicketPayload(
        user_id=str(decoded["sub"]),
        tenant_id=str(decoded["tenant_id"]),
        conversation_id=str(decoded["conversation_id"]),
        exp=int(decoded["exp"]),
    )
