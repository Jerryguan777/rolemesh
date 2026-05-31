"""Configuration for the WebUI FastAPI service."""

from __future__ import annotations

import os
from pathlib import Path

NATS_URL: str = os.environ.get("NATS_URL", "nats://localhost:4222")
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://rolemesh:rolemesh@localhost:5432/rolemesh",
)
WEB_UI_PORT: int = int(os.environ.get("WEB_UI_PORT", "8080"))
WEB_UI_HOST: str = os.environ.get("WEB_UI_HOST", "0.0.0.0")
WEB_UI_DIST: Path = Path(os.environ.get("WEB_UI_DIST", "web/dist"))
ADMIN_BOOTSTRAP_TOKEN: str = os.environ.get("ADMIN_BOOTSTRAP_TOKEN", "")

# Signing key for /api/v1/auth/ws-ticket. Separate from the API
# session JWT so a leak of one doesn't compromise the other. The
# canonical reader lives in rolemesh.auth.ws_ticket; this constant
# is exported so callers building deploy manifests can grep for
# the config key in one place.
WS_TICKET_SECRET: str = os.environ.get("WS_TICKET_SECRET", "")

# Base URL of the WebUI — used to build links back to the WebUI in
# outbound messages. Empty disables link generation.
WEBUI_BASE_URL: str = os.environ.get("WEBUI_BASE_URL", "").rstrip("/")

# --- OIDC configuration (used when AUTH_MODE=oidc) ---
# Convenience re-export so existing webui imports keep working. The source of
# truth is rolemesh.auth.oidc.config; new code may import from either location.
from rolemesh.auth.oidc.config import (  # noqa: E402
    OIDC_ADAPTER,
    OIDC_AUDIENCE,
    OIDC_CLAIM_ROLE,
    OIDC_CLAIM_TENANT_ID,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_COOKIE_SAMESITE,
    OIDC_COOKIE_SECURE,
    OIDC_DISCOVERY_URL,
    OIDC_REDIRECT_URI,
    OIDC_REFRESH_COOKIE_TTL,
    OIDC_SCOPE_ROLE_MAP,
    OIDC_SCOPES,
)

__all__ = [
    "OIDC_ADAPTER",
    "OIDC_AUDIENCE",
    "OIDC_CLAIM_ROLE",
    "OIDC_CLAIM_TENANT_ID",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_COOKIE_SAMESITE",
    "OIDC_COOKIE_SECURE",
    "OIDC_DISCOVERY_URL",
    "OIDC_REDIRECT_URI",
    "OIDC_REFRESH_COOKIE_TTL",
    "OIDC_SCOPES",
    "OIDC_SCOPE_ROLE_MAP",
]

# CORS allowed origins (comma-separated). Required for embedded SaaS scenarios
# where the browser sends credentials cross-origin.
CORS_ORIGINS: list[str] = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
