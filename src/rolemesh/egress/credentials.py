"""DB-backed per-tenant credential resolver for the egress proxy.

Bridges the existing :class:`rolemesh.auth.credential_vault.CredentialVault`
primitive and the ``tenant_model_credentials`` table into per-request
lookups. The hot path is cached 60s by default so Fernet decrypt isn't
re-paid on every chunk of a streaming LLM response.

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
from typing import TYPE_CHECKING, Any

from rolemesh.db.model import get_credential_ciphertext

if TYPE_CHECKING:
    from rolemesh.auth.credential_vault import CredentialVault


__all__ = [
    "CredentialResolver",
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


class CredentialResolver:
    """In-memory cache over ``(tenant_id, provider) → decrypted dict``."""

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

        blob = await get_credential_ciphertext(tenant_id, provider)
        if blob is None:
            raise MissingCredentialError(tenant_id, provider)

        decrypted = self._vault.decrypt_json(blob)
        self._cache[key] = (decrypted, now + self._ttl)
        return decrypted
