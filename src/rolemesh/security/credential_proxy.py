"""HTTP credential proxy — legacy import path (kept for compatibility).

The business logic moved to ``rolemesh.egress.reverse_proxy`` in EC-2.
This module stays behind as a thin re-export so every historical
``from rolemesh.security.credential_proxy import ...`` call site
continues to resolve.

Do NOT add new functionality here — extend ``rolemesh.egress.reverse_proxy``
instead and add a re-export below if the new symbol needs to be
reachable from the old path.
"""

from __future__ import annotations

from rolemesh.egress.reverse_proxy import (
    AuthMode,
    detect_auth_mode,
    get_mcp_registry,
    register_mcp_server,
    set_token_vault,
    start_credential_proxy,
)

__all__ = [
    "AuthMode",
    "detect_auth_mode",
    "get_mcp_registry",
    "register_mcp_server",
    "set_token_vault",
    "start_credential_proxy",
]
