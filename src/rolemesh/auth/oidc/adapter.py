"""OIDC claim-mapping adapter Protocol and default implementation."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol, runtime_checkable

from rolemesh.core.logger import get_logger

logger = get_logger()

_VALID_ROLES = ("owner", "admin", "member")


@runtime_checkable
class OIDCAdapter(Protocol):
    """Pluggable adapter for IdP-specific claim mapping and provisioning hooks.

    Implementations can be customized per deployment by setting the
    OIDC_ADAPTER env var to a module path (e.g. "myapp.adapters.MyAdapter").
    """

    def map_role(self, claims: dict[str, Any]) -> str:
        """Map IdP claims/scopes to a RoleMesh role: owner / admin / member."""
        ...

    def map_tenant_id(self, claims: dict[str, Any]) -> str:
        """Extract the external tenant identifier from claims (empty for single-tenant)."""
        ...

    async def on_tenant_provisioned(
        self, tenant_id: str, claims: dict[str, Any]
    ) -> None:
        """Hook called after JIT-creating a new tenant."""
        ...

    async def on_user_provisioned(
        self, user_id: str, tenant_id: str, claims: dict[str, Any]
    ) -> None:
        """Hook called after JIT-creating a new user."""
        ...


class DefaultOIDCAdapter:
    """Default adapter with explicit configuration.

    Args:
        claim_role: Direct role claim name (priority over scope mapping).
        scope_role_map: {scope: role} mapping for scope-based role resolution.
        claim_tenant_id: Claim name carrying the IdP tenant identifier.

    All parameters default to empty/None for single-tenant deployments
    where the IdP issues a single role claim or no role at all.
    """

    def __init__(
        self,
        claim_role: str = "",
        scope_role_map: dict[str, str] | None = None,
        claim_tenant_id: str = "",
    ) -> None:
        self._claim_role = claim_role
        self._claim_tenant = claim_tenant_id
        # Validate values: silently dropping config is the default OIDC failure
        # mode that gives every user 'member' role with no log — surface it loudly.
        validated: dict[str, str] = {}
        for scope, role in (scope_role_map or {}).items():
            if role in _VALID_ROLES:
                validated[scope] = role
            else:
                logger.error(
                    "OIDC scope_role_map value rejected; not in (owner|admin|member)",
                    scope=scope,
                    role=role,
                )
        self._scope_map: dict[str, str] = validated

    @classmethod
    def from_env(cls) -> DefaultOIDCAdapter:
        """Build a DefaultOIDCAdapter from OIDC_* environment variables."""
        scope_map_raw = os.environ.get("OIDC_SCOPE_ROLE_MAP", "")
        scope_map: dict[str, str] = {}
        if scope_map_raw:
            try:
                parsed = json.loads(scope_map_raw)
                if isinstance(parsed, dict):
                    scope_map = {str(k): str(v) for k, v in parsed.items()}
                else:
                    logger.error(
                        "OIDC_SCOPE_ROLE_MAP must be a JSON object", value=scope_map_raw
                    )
            except json.JSONDecodeError:
                logger.error("Invalid OIDC_SCOPE_ROLE_MAP JSON", value=scope_map_raw)
        return cls(
            claim_role=os.environ.get("OIDC_CLAIM_ROLE", ""),
            scope_role_map=scope_map,
            claim_tenant_id=os.environ.get("OIDC_CLAIM_TENANT_ID", ""),
        )

    def map_role(self, claims: dict[str, Any]) -> str:
        # 1. Direct role claim takes priority
        if self._claim_role:
            role = claims.get(self._claim_role)
            if isinstance(role, str) and role in _VALID_ROLES:
                return role
        # 2. Scope-based mapping — highest-privilege scope wins
        if self._scope_map:
            scope_value = claims.get("scope") or claims.get("scp") or ""
            scopes = scope_value if isinstance(scope_value, list) else str(scope_value).split()
            mapped_roles = {self._scope_map[s] for s in scopes if s in self._scope_map}
            for role in _VALID_ROLES:
                if role in mapped_roles:
                    return role
            # Configured scope_map exists but matched nothing — likely a config
            # mismatch between IdP scopes and OIDC_SCOPE_ROLE_MAP keys.
            logger.warning(
                "OIDC scope_role_map matched no scopes; defaulting to 'member'",
                token_scopes=list(scopes),
                configured_scopes=list(self._scope_map.keys()),
            )
        return "member"

    def map_tenant_id(self, claims: dict[str, Any]) -> str:
        if not self._claim_tenant:
            return ""
        value = claims.get(self._claim_tenant, "")
        return str(value) if value else ""

    async def on_tenant_provisioned(
        self, tenant_id: str, claims: dict[str, Any]
    ) -> None:
        pass

    async def on_user_provisioned(
        self, user_id: str, tenant_id: str, claims: dict[str, Any]
    ) -> None:
        pass
