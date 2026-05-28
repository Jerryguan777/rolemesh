"""DB-backed per-tenant credential resolver for the egress proxy.

Bridges the existing :class:`rolemesh.auth.credential_vault.CredentialVault`
primitive and the ``tenant_model_credentials`` table into per-request
lookups. The hot path is cached 60s by default so Fernet decrypt isn't
re-paid on every chunk of a streaming LLM response.

Two concrete resolvers satisfy :class:`CredentialResolverProtocol`:

- :class:`CredentialResolver` (this module): the orchestrator-side
  implementation. Holds a real :class:`CredentialVault` and reads
  ``tenant_model_credentials`` directly. Used by the host-side
  proxy bound on 127.0.0.1.
- :class:`rolemesh.egress.remote_credentials.RemoteCredentialResolver`:
  the gateway-side implementation. The egress-gateway container ships
  without ``rolemesh.db`` or ``rolemesh.auth`` (stateless boundary),
  so it forwards each resolve over a NATS RPC to the orchestrator.

The two share this module's :class:`MissingCredentialError` so the
reverse proxy can fail-close on either.

Cache invalidation is plain wall-clock TTL — no NATS event subscription.
A credential PUT on the UI takes up to ``ttl_seconds`` to apply at
runtime, which is acceptable for the dev / current-stage threat model.
Promoting to event-driven invalidation is a separate chore if a
production deployment needs sub-second propagation.

Failure modes are explicit:

- DB miss → :class:`MissingCredentialError`. The exception carries
  only ``tenant_id`` and ``provider`` — never the requested API key
  shape, ciphertext, or plaintext.
- Fernet decrypt failure → :class:`cryptography.fernet.InvalidToken`
  propagates uncaught. Callers (the reverse proxy) translate this
  into 503 per design §8.1.2: a wrong master key is operator-level
  unrecoverable and should not look like a transient 500.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from rolemesh.auth.credential_vault import CredentialVault


__all__ = [
    "CredentialResolver",
    "CredentialResolverProtocol",
    "MissingCredentialError",
]


class MissingCredentialError(Exception):
    """No credential row exists for ``(tenant_id, provider)``.

    Constructor takes only the identifying tuple by design — leaving
    no place for a future maintainer to attach plaintext or ciphertext
    to the exception message "for debugging".
    """

    def __init__(self, tenant_id: str, provider: str) -> None:
        self.tenant_id = tenant_id
        self.provider = provider
        super().__init__(
            f"No credential for tenant={tenant_id} provider={provider}"
        )


@runtime_checkable
class CredentialResolverProtocol(Protocol):
    """Structural contract for the reverse proxy's credential lookup.

    Concrete implementations differ in where the bytes come from
    (local DB vs orchestrator over NATS) but the wire shape — input
    tuple + dict return + ``MissingCredentialError`` on absence — is
    common.
    """

    async def resolve(
        self, tenant_id: str, provider: str
    ) -> dict[str, Any]: ...


class CredentialResolver:
    """DB + vault implementation of :class:`CredentialResolverProtocol`.

    Imported into the orchestrator process where both modules are
    available. The egress-gateway container loads
    :class:`RemoteCredentialResolver` instead — the gateway image
    excludes ``rolemesh.db`` by design (EC-1 stateless boundary).
    """

    def __init__(
        self, vault: CredentialVault, *, ttl_seconds: int = 60
    ) -> None:
        self._vault = vault
        self._ttl = ttl_seconds
        self._cache: dict[
            tuple[str, str], tuple[dict[str, Any], float]
        ] = {}

    async def resolve(
        self, tenant_id: str, provider: str
    ) -> dict[str, Any]:
        """Return the decrypted credential dict for one tenant + provider.

        Raises :class:`MissingCredentialError` if the row is absent.
        Lets :class:`cryptography.fernet.InvalidToken` propagate on a
        wrong master key — see module docstring.
        """
        key = (tenant_id, provider)
        now = time.monotonic()

        cached = self._cache.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]

        # Lazy import — the gateway-side ``credentials.py`` is imported
        # by reverse_proxy at module load even though the gateway never
        # constructs :class:`CredentialResolver` (it uses
        # :class:`RemoteCredentialResolver`). Pulling ``rolemesh.db``
        # at the module top would crash the gateway on boot.
        from rolemesh.db.model import get_credential_ciphertext

        blob = await get_credential_ciphertext(tenant_id, provider)
        if blob is None:
            raise MissingCredentialError(tenant_id, provider)

        decrypted = self._vault.decrypt_json(blob)
        self._cache[key] = (decrypted, now + self._ttl)
        return decrypted
