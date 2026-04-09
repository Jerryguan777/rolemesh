"""Factory for creating AuthProvider instances based on deployment mode."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthProvider


def create_auth_provider(mode: str = "") -> AuthProvider:
    """Create an AuthProvider for the given deployment mode.

    Args:
        mode: "external", "builtin", or "oidc". If empty, reads AUTH_MODE env var.
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

    if mode == "oidc":
        from rolemesh.auth.oidc.adapter import DefaultOIDCAdapter
        from rolemesh.auth.oidc.config import OIDCConfig
        from rolemesh.auth.oidc.provider import OIDCAuthProvider

        cfg = OIDCConfig.from_env()

        # Optional custom adapter via OIDC_ADAPTER=module.path.ClassName
        if cfg.adapter_spec:
            import importlib

            module_path, _, class_name = cfg.adapter_spec.rpartition(".")
            if not module_path:
                raise ValueError(f"Invalid OIDC_ADAPTER spec: {cfg.adapter_spec!r}")
            module = importlib.import_module(module_path)
            adapter_cls = getattr(module, class_name)
            adapter = adapter_cls()
        else:
            adapter = DefaultOIDCAdapter.from_config(cfg)

        return OIDCAuthProvider(cfg, adapter)  # type: ignore[return-value]

    raise ValueError(f"Unknown auth mode: {mode!r}. Expected 'external', 'builtin', or 'oidc'.")
