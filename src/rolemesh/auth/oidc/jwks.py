"""JWKS fetching and caching with key-rotation handling."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx
import jwt

from rolemesh.auth.oidc.discovery import DiscoveryDocument
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    # NOTE: AllowedPublicKeys lives in jwt.algorithms, not jwt's public surface.
    # If a future PyJWT release moves it, swap to a local Union of cryptography
    # public-key types (RSAPublicKey | EllipticCurvePublicKey | Ed25519PublicKey
    # | Ed448PublicKey). TYPE_CHECKING-only, so runtime is unaffected.
    from jwt.algorithms import AllowedPublicKeys

logger = get_logger()


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
        self._discovery: DiscoveryDocument | None = None
        self._jwks: dict[str, Any] = {}  # kid -> jwk dict
        self._jwks_fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def discovery(self) -> DiscoveryDocument:
        """Return the cached discovery document, fetching if expired."""
        async with self._lock:
            return await self._discovery_locked()

    async def _discovery_locked(self) -> DiscoveryDocument:
        """Internal: fetch discovery doc. Caller must hold _lock."""
        if self._discovery is None or (time.time() - self._discovery.fetched_at) > self._cache_ttl:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._discovery_url)
                resp.raise_for_status()
                data = resp.json()
            self._discovery = DiscoveryDocument(
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

    async def get_signing_key(self, kid: str) -> AllowedPublicKeys:
        """Return the public key for the given kid, refreshing JWKS if not found."""
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
