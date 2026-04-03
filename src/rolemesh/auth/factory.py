"""Factory for creating AuthProvider instances based on deployment mode."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthProvider


def create_auth_provider(mode: str = "") -> AuthProvider:
    """Create an AuthProvider for the given deployment mode.

    Args:
        mode: "embedded" or "standalone". If empty, reads AUTH_MODE env var.

    Returns:
        An AuthProvider instance.

    Raises:
        ValueError: If the mode is unknown.
    """
    if not mode:
        mode = os.environ.get("AUTH_MODE", "embedded")

    if mode == "embedded":
        from rolemesh.auth.embedded_provider import EmbeddedProvider

        return EmbeddedProvider()  # type: ignore[return-value]

    if mode == "standalone":
        from rolemesh.auth.standalone_provider import StandaloneProvider

        return StandaloneProvider()  # type: ignore[return-value]

    raise ValueError(f"Unknown auth mode: {mode!r}. Expected 'embedded' or 'standalone'.")
