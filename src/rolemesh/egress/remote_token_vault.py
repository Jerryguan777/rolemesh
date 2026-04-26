"""NATS-backed TokenVault proxy used by the egress gateway.

Why this exists
---------------
The DB-backed ``rolemesh.auth.token_vault.TokenVault`` reads encrypted
refresh tokens from the ``oidc_user_tokens`` table and POSTs to the
IdP token endpoint to refresh them. Both operations require modules
the egress-gateway container deliberately doesn't ship — ``rolemesh.db``
is not COPYed into ``Dockerfile.egress-gateway`` and we don't want to
distribute DB credentials to the gateway. EC-1..EC-3's invariant is
"gateway is a network-layer proxy; it does not hold persistent state".

Solution: the gateway calls ``RemoteTokenVault.get_fresh_access_token``,
which forwards the request over a NATS request-reply RPC to an
orchestrator-side responder (see ``orch_glue.start_token_responder``).
The orchestrator already has a real ``TokenVault`` instance plus DB
access; it does the work and returns the access_token (or null on
failure / unknown user). All caches and IdP traffic stay on the
orchestrator process.

Wire format
-----------
Subject: ``egress.token.access.request``
Request body (JSON): ``{"user_id": "<uuid>"}``
Reply body (JSON): ``{"access_token": "..."}`` or
                   ``{"access_token": null, "error": "<short>"}``

Replies are best-effort — a timeout, a NULL reply, or any
unparseable response all degrade to ``None``, which causes
``handle_mcp_proxy`` to skip Bearer injection (same posture as the
``_token_vault is None`` path it replaces).

Latency note
------------
Each user-mode MCP request adds one round-trip on this RPC.
Measured against LLM tool-call paths the cost is in the noise; if it
ever becomes hot, the next iteration adds an in-gateway TTL cache
keyed on user_id (token validity is already minutes-scale on the
orchestrator side, so a 30s gateway cache would absorb most spikes).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    import nats.aio.client

logger = get_logger()


# NATS subject owned by the token-vault RPC. Singular ``request``
# rather than the plural other egress subjects use because the RPC
# is per-call (one user_id → one access_token), not a snapshot
# stream.
TOKEN_ACCESS_REQUEST_SUBJECT = "egress.token.access.request"

# Default per-call timeout. Generous — the orchestrator side may
# need to make an outbound IdP refresh call, which can sit at the
# 1-2s range under typical OIDC providers. We err high here because
# a timeout-induced None makes the user's MCP request fail with 401;
# falsely racing the IdP is worse than waiting one extra second.
_DEFAULT_TIMEOUT_S = 5.0


class RemoteTokenVault:
    """``TokenVaultProtocol`` implementation that forwards token
    requests to the orchestrator over NATS.

    Implements only ``get_fresh_access_token`` — the gateway never
    needs ``store_initial`` or ``revoke`` (those run from the WebUI's
    OIDC callback path against the local ``TokenVault``).
    """

    def __init__(
        self,
        nats_client: nats.aio.client.Client,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._nc = nats_client
        self._timeout_s = timeout_s

    async def get_fresh_access_token(self, user_id: str) -> str | None:
        """Ask the orchestrator for a fresh access_token for ``user_id``.

        Returns None on:
          - empty/falsy user_id (defensive — protocol level)
          - NATS RPC timeout / transport error
          - orchestrator reports the user has no stored tokens
          - orchestrator's IdP refresh failed
          - any malformed reply

        These all map to ""no Bearer header injected"" upstream, which
        is the same fallback the ``_token_vault is None`` path always
        produced before this class existed.
        """
        if not user_id:
            return None

        body = json.dumps({"user_id": user_id}).encode("utf-8")
        try:
            response = await self._nc.request(  # type: ignore[attr-defined]
                TOKEN_ACCESS_REQUEST_SUBJECT,
                body,
                timeout=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort: any
            # transport error degrades to None and the MCP request
            # surfaces a 401 from the upstream service.
            logger.warning(
                "remote_token_vault: RPC failed",
                user_id=user_id,
                error=str(exc),
            )
            return None

        try:
            payload = json.loads(response.data)
        except (ValueError, AttributeError) as exc:
            logger.warning(
                "remote_token_vault: malformed reply",
                user_id=user_id,
                error=str(exc),
            )
            return None

        if not isinstance(payload, dict):
            logger.warning(
                "remote_token_vault: reply not a dict",
                user_id=user_id,
                got=type(payload).__name__,
            )
            return None

        access_token = payload.get("access_token")
        if access_token is None:
            # Orchestrator may have included a hint about why. Surface
            # at debug — operators see info via the orchestrator log.
            err = payload.get("error")
            if err:
                logger.debug(
                    "remote_token_vault: no token returned",
                    user_id=user_id,
                    reason=str(err),
                )
            return None

        if not isinstance(access_token, str):
            logger.warning(
                "remote_token_vault: access_token not a string",
                user_id=user_id,
                got=type(access_token).__name__,
            )
            return None

        return access_token
