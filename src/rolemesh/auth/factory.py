"""Factory for creating AuthProvider instances based on deployment mode."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthProvider


def create_auth_provider(mode: str = "") -> AuthProvider:
    """Create an AuthProvider for the given deployment mode.

    Args:
        mode: "external" or "builtin". If empty, reads AUTH_MODE env var.
              Legacy values "embedded" and "standalone" are accepted as aliases.

    Returns:
        An AuthProvider instance.

    Raises:
        ValueError: If the mode is unknown.
    """
    if not mode:
        mode = os.environ.get("AUTH_MODE", "external")

    # Accept legacy aliases
    if mode == "embedded":
        mode = "external"
    elif mode == "standalone":
        mode = "builtin"

    if mode == "external":
        from rolemesh.auth.external_jwt_provider import ExternalJwtProvider

        return ExternalJwtProvider()  # type: ignore[return-value]

    if mode == "builtin":
        from rolemesh.auth.builtin_provider import BuiltinProvider

        return BuiltinProvider()  # type: ignore[return-value]

    raise ValueError(f"Unknown auth mode: {mode!r}. Expected 'external' or 'builtin'.")
