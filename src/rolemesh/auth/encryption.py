"""Shared Fernet-key derivation for application vaults.

Two vaults derive keys from environment-provided secrets:

* :class:`rolemesh.auth.token_vault.TokenVault` — OIDC refresh /
  access tokens (env var ``ROLEMESH_TOKEN_SECRET``).
* :class:`rolemesh.auth.credential_vault.CredentialVault` — tenant
  LLM provider API keys (env var ``CREDENTIAL_VAULT_KEY``).

Before this module existed both vaults inlined the same SHA-256 +
base64 derivation; design §8.1 calls for a single helper so the two
vaults cannot silently drift on key length, encoding, or normalisation.
``TokenVault.derive_key`` is now a thin alias that calls
:func:`derive_fernet_key` so existing call-sites keep working without
changing their import.

No silent fallbacks. An unset / empty secret raises — :func:`load_vault_key_from_env`
fails the process boot so a misconfigured deploy cannot accidentally
encrypt with an empty key (which Fernet would still happily accept).
"""

from __future__ import annotations

import base64
import hashlib
import os

__all__ = [
    "derive_fernet_key",
    "load_vault_key_from_env",
]


def derive_fernet_key(secret: str) -> bytes:
    """Return a Fernet-compatible 32-byte urlsafe-base64 key.

    SHA-256 collapses arbitrary-length operator-chosen secrets to a
    fixed 32-byte digest; Fernet then requires the urlsafe-base64
    encoding of that 32-byte block. The encoding is identity-preserving
    so equal secrets produce equal keys — needed because both processes
    (orchestrator + webui) derive independently and must land on the
    same key.
    """
    if not secret:
        raise ValueError("derive_fernet_key requires a non-empty secret")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def load_vault_key_from_env(env_var: str) -> bytes:
    """Read ``env_var`` from the process env and derive a Fernet key.

    Raises ``RuntimeError`` if the variable is unset or empty so the
    caller can surface the failure as a fail-loud boot error rather
    than silently constructing a vault that cannot decrypt.
    """
    secret = os.environ.get(env_var, "")
    if not secret:
        raise RuntimeError(
            f"Environment variable {env_var!r} is not set; "
            "refusing to construct a vault without an explicit master key."
        )
    return derive_fernet_key(secret)
