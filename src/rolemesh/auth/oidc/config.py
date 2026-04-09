"""OIDC configuration read from environment variables.

Lives in the auth subpackage so that backend processes (orchestrator,
credential proxy) can read OIDC config without importing webui.
"""

from __future__ import annotations

import os

OIDC_DISCOVERY_URL: str = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_CLIENT_ID: str = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET: str = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_AUDIENCE: str = os.environ.get("OIDC_AUDIENCE", "")  # defaults to client_id
OIDC_REDIRECT_URI: str = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES: str = os.environ.get("OIDC_SCOPES", "openid profile email")
OIDC_SCOPE_ROLE_MAP: str = os.environ.get("OIDC_SCOPE_ROLE_MAP", "")  # JSON
OIDC_CLAIM_ROLE: str = os.environ.get("OIDC_CLAIM_ROLE", "")
OIDC_CLAIM_TENANT_ID: str = os.environ.get("OIDC_CLAIM_TENANT_ID", "")
OIDC_ADAPTER: str = os.environ.get("OIDC_ADAPTER", "")  # module path

# Refresh token cookie configuration
OIDC_COOKIE_SAMESITE: str = os.environ.get("OIDC_COOKIE_SAMESITE", "lax")  # lax | strict | none
OIDC_COOKIE_SECURE: bool = os.environ.get("OIDC_COOKIE_SECURE", "true").lower() == "true"
OIDC_REFRESH_COOKIE_TTL: int = int(os.environ.get("OIDC_REFRESH_COOKIE_TTL", str(30 * 86400)))
