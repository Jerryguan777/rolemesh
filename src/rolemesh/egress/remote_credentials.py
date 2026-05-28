"""NATS-backed CredentialResolver proxy used by the egress gateway.

Why this exists
---------------
The DB-backed :class:`rolemesh.egress.credentials.CredentialResolver`
reads encrypted blobs from ``tenant_model_credentials`` and Fernet-
decrypts them. Both operations require modules the egress-gateway
container deliberately doesn't ship — ``rolemesh.db`` and
``rolemesh.auth`` are not COPYed into ``Dockerfile.egress-gateway``
and the gateway doesn't hold the master vault key. EC-1's invariant
is "gateway is a network-layer proxy; it does not hold persistent
state or master secrets".

Mirrors the sibling :class:`rolemesh.egress.remote_token_vault.
RemoteTokenVault` shape — both forward to an orchestrator-side
responder which already has DB + vault access.

Wire format
-----------
Subject: ``egress.credential.request``

Request body (JSON)::

    {"tenant_id": "<uuid>", "provider": "<name>"}

Reply body (JSON), one of::

    {"credential": {"api_key": "...", "extras": {...}}}
    {"credential": null, "error": "MISSING"}     # no row in DB
    {"credential": null, "error": "<other>"}     # unexpected server-side fault

A transport timeout, malformed reply, or ``error`` other than
``MISSING`` surfaces as :class:`RuntimeError` so the reverse proxy
returns 502 (server fault) rather than 401 (caller's credential
missing). The two failure modes deserve different operator
responses — a 401 says "configure the credential", a 502 says
"orchestrator is broken".

Cache
-----
Same 60s in-memory TTL as the DB-backed resolver. Two-process cache
(gateway + orchestrator-side) means a credential PUT propagates as
late as ``orchestrator.ttl + gateway.ttl`` = up to ~2 minutes. That
ceiling is acceptable for the current dev posture; sub-second
propagation is a separate chore.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

from .credentials import MissingCredentialError

if TYPE_CHECKING:
    import nats.aio.client

logger = get_logger()


# NATS subject owned by the credential RPC. Singular ``request``
# rather than the plural ``*.snapshot.request`` subjects use because
# the RPC is per-call (one (tenant_id, provider) → one credential
# dict), not a snapshot stream.
CREDENTIAL_REQUEST_SUBJECT = "egress.credential.request"

# Default per-call timeout. Generous — the orchestrator side may need
# to wait on a DB query + Fernet decrypt. A timeout-induced failure
# surfaces as 502 upstream, which is the same posture as any other
# orchestrator-unreachable failure mode.
_DEFAULT_TIMEOUT_S = 5.0


__all__ = [
    "CREDENTIAL_REQUEST_SUBJECT",
    "RemoteCredentialResolver",
]


class RemoteCredentialResolver:
    """Gateway-side resolver that forwards every lookup over NATS.

    Implements :class:`rolemesh.egress.credentials.CredentialResolverProtocol`
    structurally — the reverse proxy treats this and the DB-backed
    :class:`CredentialResolver` interchangeably.
    """

    def __init__(
        self,
        nats_client: nats.aio.client.Client,
        *,
        ttl_seconds: int = 60,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._nc = nats_client
        self._ttl = ttl_seconds
        self._timeout_s = timeout_s
        self._cache: dict[
            tuple[str, str], tuple[dict[str, Any], float]
        ] = {}

    async def resolve(
        self, tenant_id: str, provider: str
    ) -> dict[str, Any]:
        """Ask the orchestrator for the credential for (tenant, provider).

        Caches on success for ``ttl_seconds``. Does NOT cache failures
        (negative caching would amplify a momentary outage into a
        ~minute-long denial).

        Raises:
            MissingCredentialError: the orchestrator reports no row.
            RuntimeError: transport timeout, malformed reply, or any
                server-side fault that isn't ``MISSING``.
        """
        key = (tenant_id, provider)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        body = json.dumps(
            {"tenant_id": tenant_id, "provider": provider},
        ).encode("utf-8")
        try:
            response = await self._nc.request(
                CREDENTIAL_REQUEST_SUBJECT,
                body,
                timeout=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — any transport
            # failure surfaces as 502 upstream (RuntimeError chain).
            logger.warning(
                "remote_credentials: RPC failed",
                tenant_id=tenant_id,
                provider=provider,
                error=str(exc),
            )
            raise RuntimeError(
                f"credential RPC failed for ({tenant_id}, {provider}): {exc}"
            ) from exc

        try:
            payload = json.loads(response.data)
        except (ValueError, AttributeError) as exc:
            raise RuntimeError(
                f"credential RPC malformed reply for "
                f"({tenant_id}, {provider}): {exc}"
            ) from exc

        if not isinstance(payload, dict):
            raise RuntimeError(
                f"credential RPC reply not a dict for "
                f"({tenant_id}, {provider}): {type(payload).__name__}"
            )

        credential = payload.get("credential")
        if credential is None:
            err = payload.get("error")
            if err == "MISSING":
                raise MissingCredentialError(tenant_id, provider)
            raise RuntimeError(
                f"credential RPC returned no credential for "
                f"({tenant_id}, {provider}); error={err!r}"
            )

        if not isinstance(credential, dict):
            raise RuntimeError(
                f"credential RPC credential not a dict for "
                f"({tenant_id}, {provider}): {type(credential).__name__}"
            )

        self._cache[key] = (credential, now + self._ttl)
        return credential
