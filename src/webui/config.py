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

# --- OIDC configuration (used when AUTH_MODE=oidc) ---
OIDC_DISCOVERY_URL: str = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_CLIENT_ID: str = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET: str = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI: str = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES: str = os.environ.get("OIDC_SCOPES", "openid profile email")
OIDC_AUDIENCE: str = os.environ.get("OIDC_AUDIENCE", "")  # defaults to client_id
OIDC_SCOPE_ROLE_MAP: str = os.environ.get("OIDC_SCOPE_ROLE_MAP", "")  # JSON
OIDC_CLAIM_ROLE: str = os.environ.get("OIDC_CLAIM_ROLE", "")
OIDC_CLAIM_TENANT_ID: str = os.environ.get("OIDC_CLAIM_TENANT_ID", "")
OIDC_ADAPTER: str = os.environ.get("OIDC_ADAPTER", "")  # module path, e.g. "myapp.adapters.MyAdapter"
