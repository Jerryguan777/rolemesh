"""PKCE utilities for OAuth flows.

Ported from packages/ai/src/utils/oauth/pkce.ts.
Uses Python's secrets and hashlib for cross-platform compatibility.
"""

from __future__ import annotations

import base64
import hashlib
import secrets


def _base64url_encode(data: bytes) -> str:
    """Encode bytes as base64url string (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


async def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge.

    Returns (verifier, challenge).
    """
    # Generate random verifier (32 bytes -> base64url encoded)
    verifier_bytes = secrets.token_bytes(32)
    verifier = _base64url_encode(verifier_bytes)

    # Compute SHA-256 challenge
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _base64url_encode(digest)

    return verifier, challenge
