"""OIDC claim-mapping adapter Protocol and default implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.auth.oidc.config import OIDCConfig

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
        claim_groups: Claim name carrying the user's group memberships.
        group_role_map: {group: role} mapping for group-based role resolution.

    All parameters default to empty/None for single-tenant deployments
    where the IdP issues a single role claim or no role at all.
    """

    def __init__(
        self,
        claim_role: str = "",
        scope_role_map: dict[str, str] | None = None,
        claim_tenant_id: str = "",
        claim_groups: str = "",
        group_role_map: dict[str, str] | None = None,
    ) -> None:
        self._claim_role = claim_role
        self._claim_tenant = claim_tenant_id
        self._claim_groups = claim_groups
        # Validate values: silently dropping config is the default OIDC failure
        # mode that gives every user 'member' role with no log — surface it loudly.
        self._scope_map: dict[str, str] = self._validate_role_map(
            scope_role_map, "scope_role_map"
        )
        self._group_map: dict[str, str] = self._validate_role_map(
            group_role_map, "group_role_map"
        )

    @staticmethod
    def _validate_role_map(
        raw: dict[str, str] | None, name: str
    ) -> dict[str, str]:
        validated: dict[str, str] = {}
        for key, role in (raw or {}).items():
            if role in _VALID_ROLES:
                validated[key] = role
            else:
                logger.error(
                    "OIDC role map value rejected; not in (owner|admin|member)",
                    map_name=name,
                    key=key,
                    role=role,
                )
        return validated

    @classmethod
    def from_config(cls, cfg: OIDCConfig) -> DefaultOIDCAdapter:
        """Build a DefaultOIDCAdapter from a structured OIDCConfig."""
        return cls(
            claim_role=cfg.claim_role,
            scope_role_map=cfg.scope_role_map,
            claim_tenant_id=cfg.claim_tenant_id,
            claim_groups=cfg.claim_groups,
            group_role_map=cfg.group_role_map,
        )

    @classmethod
    def from_env(cls) -> DefaultOIDCAdapter:
        """Build a DefaultOIDCAdapter from OIDC_* environment variables.

        Convenience wrapper: parses env into OIDCConfig, then delegates to
        from_config so that JSON parsing happens in one place.
        """
        from rolemesh.auth.oidc.config import OIDCConfig

        return cls.from_config(OIDCConfig.from_env())

    def map_role(self, claims: dict[str, Any]) -> str:
        # 1. Direct role claim takes priority
        if self._claim_role:
            role = claims.get(self._claim_role)
            if isinstance(role, str) and role in _VALID_ROLES:
                return role

        # 2. Group-based mapping (enterprise IdPs put role signal in `groups`).
        # Many id_tokens carry no `scope` claim at all — groups is the only
        # signal. Highest-privilege group wins.
        if self._claim_groups and self._group_map:
            groups_value = claims.get(self._claim_groups, [])
            if isinstance(groups_value, str):
                # Some IdPs flatten to space- or comma-separated string
                groups = [g.strip() for g in groups_value.replace(",", " ").split() if g.strip()]
            elif isinstance(groups_value, list):
                groups = [str(g) for g in groups_value]
            else:
                groups = []
            mapped = {self._group_map[g] for g in groups if g in self._group_map}
            for role in _VALID_ROLES:
                if role in mapped:
                    return role
            if groups:
                logger.warning(
                    "OIDC group_role_map matched no groups; falling through",
                    token_groups=groups,
                    configured_groups=list(self._group_map.keys()),
                )

        # 3. Scope-based mapping — highest-privilege scope wins
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
