"""OIDC AuthProvider: id_token validation, claim mapping, JIT provisioning."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import jwt

from rolemesh.auth.oidc.adapter import DefaultOIDCAdapter, OIDCAdapter
from rolemesh.auth.oidc.algorithms import ALLOWED_ALGORITHMS
from rolemesh.auth.oidc.jwks import JWKSManager
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.auth.oidc.config import OIDCConfig
    from rolemesh.auth.oidc.discovery import DiscoveryDocument

logger = get_logger()


class OIDCAuthProvider:
    """OIDC-based AuthProvider.

    Validates id_tokens via JWKS, maps claims via the configured adapter,
    and JIT-provisions tenants/users on first login.
    """

    def __init__(
        self,
        config: OIDCConfig,
        adapter: OIDCAdapter | None = None,
    ) -> None:
        if not config.discovery_url:
            raise ValueError("OIDCAuthProvider requires config.discovery_url")
        if not config.client_id:
            raise ValueError("OIDCAuthProvider requires config.client_id")

        self._config = config
        self._client_id = config.client_id
        self._audience = config.audience or config.client_id
        self._provider_key = config.provider_key
        self._auto_assign_to_all = config.auto_assign_to_all
        self._jwks = JWKSManager(config.discovery_url)
        self._adapter: OIDCAdapter = adapter or DefaultOIDCAdapter()  # type: ignore[assignment]

    async def get_discovery(self) -> DiscoveryDocument:
        """Expose discovery metadata for the PKCE login endpoints."""
        return await self._jwks.discovery()

    async def authenticate(self, token: str) -> AuthenticatedUser | None:
        """Validate an id_token and JIT-provision the user/tenant."""
        if not token:
            return None
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            logger.debug("OIDC token header parse failed", error=str(exc))
            return None

        kid = unverified_header.get("kid")
        if not kid:
            logger.debug("OIDC token missing kid")
            return None

        try:
            signing_key = await self._jwks.get_signing_key(kid)
        except (jwt.InvalidTokenError, httpx.HTTPError) as exc:
            logger.warning("JWKS lookup failed", kid=kid, error=str(exc))
            return None

        try:
            disc = await self._jwks.discovery()
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=disc.issuer,
                audience=self._audience,
            )
        except jwt.InvalidTokenError as exc:
            logger.debug("OIDC token validation failed", error=str(exc))
            return None

        external_sub = claims.get("sub")
        if not external_sub:
            return None
        external_sub = str(external_sub)

        # JIT provisioning
        tenant_id = await self._provision_tenant(claims)
        if tenant_id is None:
            return None
        user = await self._provision_user(external_sub, tenant_id, claims)
        if user is None:
            return None

        return AuthenticatedUser(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
            email=user.email,
            name=user.name,
            external_token=token,
        )

    async def get_user_by_id(self, user_id: str) -> AuthenticatedUser | None:
        """Look up a previously JIT-provisioned user by local ID.

        Returns None if the user does not exist. external_token is not
        available because no token is presented for this lookup path.

        SECURITY: This lookup is NOT tenant-scoped — the AuthProvider Protocol
        does not pass tenant context. Callers MUST enforce tenant authorization
        themselves before exposing the result, otherwise a holder of any user_id
        can probe users across tenants.
        """
        from rolemesh.db.pg import get_user

        user = await get_user(user_id)
        if user is None:
            return None
        return AuthenticatedUser(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
            email=user.email,
            name=user.name,
            external_token=None,
        )

    # -- Provisioning helpers ------------------------------------------------

    async def _provision_tenant(self, claims: dict[str, Any]) -> str | None:
        from rolemesh.db.pg import (
            create_external_tenant_mapping,
            create_tenant,
            get_local_tenant_id,
            get_tenant_by_slug,
        )

        external_tenant_id = self._adapter.map_tenant_id(claims)
        if not external_tenant_id:
            # Single-tenant mode: fall back to default tenant
            default = await get_tenant_by_slug("default")
            return default.id if default else None

        local_id = await get_local_tenant_id(self._provider_key, external_tenant_id)
        if local_id is not None:
            return local_id

        # JIT-create tenant
        slug = f"oidc-{external_tenant_id}"[:60]
        tenant_name = str(claims.get("tenant_name") or external_tenant_id)
        tenant = await create_tenant(name=tenant_name, slug=slug)
        await create_external_tenant_mapping(self._provider_key, external_tenant_id, tenant.id)
        await self._adapter.on_tenant_provisioned(tenant.id, claims)
        logger.info("OIDC tenant provisioned", tenant_id=tenant.id, external_id=external_tenant_id)
        return tenant.id

    async def _provision_user(
        self, external_sub: str, tenant_id: str, claims: dict[str, Any]
    ):
        from rolemesh.db.pg import (
            create_user_with_external_sub,
            get_user_by_external_sub,
            update_user,
        )

        role = self._adapter.map_role(claims)
        email = claims.get("email")
        name = claims.get("name") or claims.get("preferred_username") or external_sub

        existing = await get_user_by_external_sub(external_sub)
        if existing is not None:
            # Sync changeable fields
            updated = await update_user(
                existing.id,
                name=str(name) if name != existing.name else None,
                email=str(email) if email and email != existing.email else None,
                role=role if role != existing.role else None,
            )
            return updated or existing

        user = await create_user_with_external_sub(
            tenant_id=tenant_id,
            name=str(name),
            email=str(email) if email else None,
            role=role,
            external_sub=external_sub,
        )
        await self._adapter.on_user_provisioned(user.id, tenant_id, claims)
        logger.info("OIDC user provisioned", user_id=user.id, sub=external_sub)

        # Auto-assign new users to every coworker in the tenant. Only fires on
        # first-time creation; subsequent logins do not re-assign because an
        # admin may have intentionally unassigned the user.
        if self._auto_assign_to_all:
            from rolemesh.db.pg import (
                assign_agent_to_user,
                get_coworkers_for_tenant,
            )

            coworkers = await get_coworkers_for_tenant(tenant_id)
            for cw in coworkers:
                await assign_agent_to_user(user.id, cw.id, tenant_id)
            logger.info(
                "OIDC user auto-assigned to coworkers",
                user_id=user.id,
                count=len(coworkers),
            )

        return user
