"""OIDC AuthProvider with JWKS validation, claim mapping, and JIT provisioning.

Validates id_tokens locally using JWKS public keys fetched from the IdP's
discovery endpoint. Supports automatic key rotation, configurable claim
mapping (via OIDCAdapter), and just-in-time tenant/user provisioning.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
import jwt

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.logger import get_logger

logger = get_logger()


# ---------------------------------------------------------------------------
# JWKS Manager
# ---------------------------------------------------------------------------


@dataclass
class _DiscoveryDocument:
    """OIDC discovery metadata cached from .well-known/openid-configuration."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    fetched_at: float


class JWKSManager:
    """Fetches and caches JWKS public keys from an OIDC discovery endpoint.

    Handles automatic refresh on key rotation: when a token's `kid` is not
    found in the cache, refresh JWKS once before failing.
    """

    def __init__(self, discovery_url: str, cache_ttl: int = 3600) -> None:
        if not discovery_url:
            raise ValueError("OIDC_DISCOVERY_URL must be set")
        self._discovery_url = discovery_url
        self._cache_ttl = cache_ttl
        self._discovery: _DiscoveryDocument | None = None
        self._jwks: dict[str, Any] = {}  # kid -> jwk dict
        self._jwks_fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def discovery(self) -> _DiscoveryDocument:
        """Return the cached discovery document, fetching if expired."""
        async with self._lock:
            return await self._discovery_locked()

    async def _discovery_locked(self) -> _DiscoveryDocument:
        """Internal: fetch discovery doc. Caller must hold _lock."""
        if self._discovery is None or (time.time() - self._discovery.fetched_at) > self._cache_ttl:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._discovery_url)
                resp.raise_for_status()
                data = resp.json()
            self._discovery = _DiscoveryDocument(
                issuer=data["issuer"],
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                jwks_uri=data["jwks_uri"],
                fetched_at=time.time(),
            )
            logger.info("OIDC discovery loaded", issuer=self._discovery.issuer)
        return self._discovery

    async def _fetch_jwks_locked(self) -> None:
        """Internal: fetch JWKS. Caller must hold _lock."""
        disc = await self._discovery_locked()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(disc.jwks_uri)
            resp.raise_for_status()
            data = resp.json()
        self._jwks = {key["kid"]: key for key in data.get("keys", []) if "kid" in key}
        self._jwks_fetched_at = time.time()
        logger.info("JWKS refreshed", key_count=len(self._jwks))

    async def get_signing_key(self, kid: str) -> Any:
        """Return the PyJWK for the given kid, refreshing JWKS if not found."""
        async with self._lock:
            cache_expired = (time.time() - self._jwks_fetched_at) > self._cache_ttl
            if not self._jwks or cache_expired:
                # Initial load or TTL expired
                await self._fetch_jwks_locked()
            elif kid not in self._jwks:
                # Cached but kid missing → IdP rotated keys, refresh once
                await self._fetch_jwks_locked()
            jwk_data = self._jwks.get(kid)
            if jwk_data is None:
                raise jwt.InvalidTokenError(f"Unknown kid: {kid}")
        return jwt.PyJWK(jwk_data).key


# ---------------------------------------------------------------------------
# OIDCAdapter Protocol
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# DefaultOIDCAdapter
# ---------------------------------------------------------------------------


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
        self._scope_map: dict[str, str] = scope_role_map or {}

    @classmethod
    def from_env(cls) -> DefaultOIDCAdapter:
        """Build a DefaultOIDCAdapter from OIDC_* environment variables."""
        scope_map_raw = os.environ.get("OIDC_SCOPE_ROLE_MAP", "")
        scope_map: dict[str, str] = {}
        if scope_map_raw:
            try:
                scope_map = json.loads(scope_map_raw)
            except json.JSONDecodeError:
                logger.warning("Invalid OIDC_SCOPE_ROLE_MAP JSON", value=scope_map_raw)
        return cls(
            claim_role=os.environ.get("OIDC_CLAIM_ROLE", ""),
            scope_role_map=scope_map,
            claim_tenant_id=os.environ.get("OIDC_CLAIM_TENANT_ID", ""),
        )

    def map_role(self, claims: dict[str, Any]) -> str:
        # 1. Direct role claim takes priority
        if self._claim_role:
            role = claims.get(self._claim_role)
            if isinstance(role, str) and role in ("owner", "admin", "member"):
                return role
        # 2. Scope-based mapping — highest-privilege scope wins
        if self._scope_map:
            scope_value = claims.get("scope") or claims.get("scp") or ""
            scopes = scope_value if isinstance(scope_value, list) else str(scope_value).split()
            mapped_roles = {self._scope_map[s] for s in scopes if s in self._scope_map}
            for role in ("owner", "admin", "member"):
                if role in mapped_roles:
                    return role
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


# ---------------------------------------------------------------------------
# OIDCAuthProvider
# ---------------------------------------------------------------------------


# Algorithms accepted for id_token signature verification.
# Whitelisted to prevent algorithm confusion attacks (e.g. tokens claiming
# alg=none or HS256 when the IdP signs with RS256).
_ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512")


class OIDCAuthProvider:
    """OIDC-based AuthProvider.

    Validates id_tokens via JWKS, maps claims via the configured adapter,
    and JIT-provisions tenants/users on first login.
    """

    PROVIDER_KEY = "oidc"

    def __init__(
        self,
        discovery_url: str,
        client_id: str,
        audience: str = "",
        adapter: OIDCAdapter | None = None,
    ) -> None:
        if not discovery_url:
            raise ValueError("OIDCAuthProvider requires discovery_url")
        if not client_id:
            raise ValueError("OIDCAuthProvider requires client_id")

        self._client_id = client_id
        self._audience = audience or client_id
        self._jwks = JWKSManager(discovery_url)
        self._adapter: OIDCAdapter = adapter or DefaultOIDCAdapter()  # type: ignore[assignment]

    async def get_discovery(self) -> _DiscoveryDocument:
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
                algorithms=list(_ALLOWED_ALGORITHMS),
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
        # Reserved for future admin/lookup flows. Not currently called by
        # the request pipeline (which only invokes authenticate()).
        return None

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

        local_id = await get_local_tenant_id(self.PROVIDER_KEY, external_tenant_id)
        if local_id is not None:
            return local_id

        # JIT-create tenant
        slug = f"oidc-{external_tenant_id}"[:60]
        tenant_name = str(claims.get("tenant_name") or external_tenant_id)
        tenant = await create_tenant(name=tenant_name, slug=slug)
        await create_external_tenant_mapping(self.PROVIDER_KEY, external_tenant_id, tenant.id)
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
        return user
