"""OIDC configuration: module-level env vars and OIDCConfig dataclass.

The module-level constants exist for legacy callers (webui re-exports them).
New code should construct an OIDCConfig via OIDCConfig.from_env() and pass
it to OIDCAuthProvider.

Cookie-related variables are intentionally NOT in OIDCConfig — they describe
how the WebUI deployment stores refresh tokens, not the IdP itself.

NOTE: All values are read at module import time. Tests that monkeypatch env
must reload this module (importlib.reload(rolemesh.auth.oidc.config)).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from rolemesh.core.logger import get_logger

logger = get_logger()

OIDC_DISCOVERY_URL: str = os.environ.get("OIDC_DISCOVERY_URL", "")
OIDC_CLIENT_ID: str = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET: str = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_AUDIENCE: str = os.environ.get("OIDC_AUDIENCE", "")  # defaults to client_id
OIDC_REDIRECT_URI: str = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES: str = os.environ.get("OIDC_SCOPES", "openid profile email")
OIDC_SCOPE_ROLE_MAP: str = os.environ.get("OIDC_SCOPE_ROLE_MAP", "")  # JSON
OIDC_CLAIM_ROLE: str = os.environ.get("OIDC_CLAIM_ROLE", "")
OIDC_CLAIM_TENANT_ID: str = os.environ.get("OIDC_CLAIM_TENANT_ID", "")
OIDC_CLAIM_GROUPS: str = os.environ.get("OIDC_CLAIM_GROUPS", "")  # claim name carrying groups
OIDC_GROUP_ROLE_MAP: str = os.environ.get("OIDC_GROUP_ROLE_MAP", "")  # JSON
OIDC_ADAPTER: str = os.environ.get("OIDC_ADAPTER", "")  # module path
OIDC_AUTO_ASSIGN_TO_ALL: bool = os.environ.get("OIDC_AUTO_ASSIGN_TO_ALL", "false").lower() == "true"

# Refresh token cookie configuration (webui-only, not part of OIDCConfig)
OIDC_COOKIE_SAMESITE: str = os.environ.get("OIDC_COOKIE_SAMESITE", "lax")  # lax | strict | none
OIDC_COOKIE_SECURE: bool = os.environ.get("OIDC_COOKIE_SECURE", "true").lower() == "true"
OIDC_REFRESH_COOKIE_TTL: int = int(os.environ.get("OIDC_REFRESH_COOKIE_TTL", str(30 * 86400)))


@dataclass(frozen=True)
class OIDCConfig:
    """Aggregated IdP configuration passed to OIDCAuthProvider.

    Groups together every IdP-level setting so a future multi-IdP layer can
    instantiate multiple providers with different configs. Cookie/transport
    settings are deliberately excluded — those belong to the WebUI deployment.
    """

    discovery_url: str
    client_id: str
    client_secret: str = ""
    audience: str = ""  # defaults to client_id when empty
    scopes: str = "openid profile email"
    redirect_uri: str = ""
    provider_key: str = "oidc"  # used as namespace key in external_tenant_map
    claim_role: str = ""
    claim_tenant_id: str = ""
    claim_groups: str = ""
    scope_role_map: dict[str, str] = field(default_factory=dict)
    group_role_map: dict[str, str] = field(default_factory=dict)
    adapter_spec: str = ""  # module.path.ClassName for OIDC_ADAPTER
    auto_assign_to_all: bool = False

    @staticmethod
    def _parse_json_map(env_name: str) -> dict[str, str]:
        """Parse a JSON env var into a string dict, logging errors."""
        raw = os.environ.get(env_name, "")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in env var", env_name=env_name, value=raw)
            return {}
        if not isinstance(parsed, dict):
            logger.error("Env var must be a JSON object", env_name=env_name, value=raw)
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    @classmethod
    def from_env(cls) -> OIDCConfig:
        """Build an OIDCConfig from OIDC_* environment variables."""
        return cls(
            discovery_url=os.environ.get("OIDC_DISCOVERY_URL", ""),
            client_id=os.environ.get("OIDC_CLIENT_ID", ""),
            client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
            audience=os.environ.get("OIDC_AUDIENCE", ""),
            scopes=os.environ.get("OIDC_SCOPES", "openid profile email"),
            redirect_uri=os.environ.get("OIDC_REDIRECT_URI", ""),
            claim_role=os.environ.get("OIDC_CLAIM_ROLE", ""),
            claim_tenant_id=os.environ.get("OIDC_CLAIM_TENANT_ID", ""),
            claim_groups=os.environ.get("OIDC_CLAIM_GROUPS", ""),
            scope_role_map=cls._parse_json_map("OIDC_SCOPE_ROLE_MAP"),
            group_role_map=cls._parse_json_map("OIDC_GROUP_ROLE_MAP"),
            adapter_spec=os.environ.get("OIDC_ADAPTER", ""),
            auto_assign_to_all=os.environ.get("OIDC_AUTO_ASSIGN_TO_ALL", "false").lower() == "true",
        )


