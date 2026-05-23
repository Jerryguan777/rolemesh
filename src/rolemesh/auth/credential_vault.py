"""LLM provider credential vault.

Encrypts the JSON payload (``{"api_key": "sk-...", ...}``) tenants
PUT through ``/api/v1/tenant/credentials/{provider}``. The ciphertext
lands in ``tenant_model_credentials.credential_data`` (BYTEA); the API
list/get surface never re-derives plaintext from this column — design
§8.1 envelope encryption model.

This module deliberately knows nothing about persistence: it is a
thin wrapper around a single Fernet, exposing
:meth:`encrypt_json` / :meth:`decrypt_json`. Routes own the DB calls.

Master key rotation (MultiFernet) is **not** implemented — design
§8.1.1 explains the deferral: LLM API keys are long-lived static
secrets, and Single-Fernet -> MultiFernet is a non-breaking upgrade
when the need arises.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet

from rolemesh.auth.encryption import load_vault_key_from_env

__all__ = [
    "CREDENTIAL_VAULT_ENV",
    "CredentialVault",
    "create_credential_vault_from_env",
    "get_credential_vault",
    "set_credential_vault",
]


CREDENTIAL_VAULT_ENV = "CREDENTIAL_VAULT_KEY"

_vault: CredentialVault | None = None


def set_credential_vault(vault: CredentialVault | None) -> None:
    """Install (or clear) the process-wide vault singleton.

    Called from each process' lifespan so :func:`get_credential_vault`
    has a value to return. Passing ``None`` is used on shutdown to make
    leak-checking explicit.
    """
    global _vault
    _vault = vault


def get_credential_vault() -> CredentialVault:
    """Return the installed vault.

    Raises ``RuntimeError`` (not :class:`AssertionError`) so a handler
    that forgets the boot-time wiring surfaces as a controlled 503,
    not an interpreter-mode crash.
    """
    if _vault is None:
        raise RuntimeError(
            "CredentialVault not installed — call set_credential_vault(...) at boot."
        )
    return _vault


class CredentialVault:
    """Symmetric envelope encryption for tenant LLM credentials.

    Single Fernet on purpose — see design §8.1.1 for the rotation
    deferral rationale. The ``encrypt_json`` / ``decrypt_json`` pair
    is the only public surface: callers serialise their dict via this
    helper rather than reaching for the underlying Fernet so the
    JSON serialisation rules stay in one place (UTF-8, sort_keys for
    deterministic ciphertext under repeated encryption of the same
    payload during tests).
    """

    def __init__(self, encryption_key: bytes) -> None:
        self._fernet = Fernet(encryption_key)

    def encrypt_json(self, data: dict[str, Any]) -> bytes:
        """Serialise ``data`` to UTF-8 JSON then Fernet-encrypt.

        Returns the Fernet token as raw bytes (the form Fernet emits) —
        callers persist this directly into the BYTEA column. Fernet
        tokens are already urlsafe-base64 so they survive any later
        text-mode round-trip if needed.
        """
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(payload)

    def decrypt_json(self, blob: bytes) -> dict[str, Any]:
        """Inverse of :meth:`encrypt_json`.

        Raises :class:`cryptography.fernet.InvalidToken` on a wrong
        key or tampered ciphertext; callers translate that to a 503
        per design §8.1.2 (a 500 would suggest a transient retry would
        help, but a wrong master key never recovers without operator
        intervention).
        """
        plaintext = self._fernet.decrypt(blob).decode("utf-8")
        decoded = json.loads(plaintext)
        if not isinstance(decoded, dict):
            raise ValueError(
                "CredentialVault.decrypt_json expected a JSON object payload"
            )
        return decoded


def create_credential_vault_from_env() -> CredentialVault:
    """Build the process-wide vault, failing loud if the key is missing.

    Called from each FastAPI / orchestrator process lifespan at boot.
    Failing here aborts the process before any handler can run — INV-
    VAULT-1 in design §8.1.3.
    """
    key = load_vault_key_from_env(CREDENTIAL_VAULT_ENV)
    return CredentialVault(key)
