"""Server-side OIDC token vault for MCP token forwarding.

Stores per-user IdP refresh_tokens (encrypted) and access_tokens, and
auto-refreshes access_tokens via the IdP's token endpoint when they
approach expiry. Used by the credential proxy to inject fresh tokens
into MCP requests.

Why not just forward the user's id_token?
  Long-running agents (30+ minutes) outlive typical 1-hour token TTLs.
  Without server-side refresh, MCP calls fail mid-execution.

Why not let the credential proxy hit the IdP for every request?
  Per-user lock + cached access_token avoid hitting IdP rate limits.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
import weakref
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.fernet import Fernet, InvalidToken

from rolemesh.core.logger import get_logger

logger = get_logger()

# Refresh access_token when it has < 60 seconds left
_REFRESH_THRESHOLD_SECONDS = 60


class TokenVault:
    """Encrypted per-user OIDC token store with automatic refresh."""

    def __init__(
        self,
        encryption_key: bytes,
        idp_token_endpoint: str,
        client_id: str,
        client_secret: str = "",
    ) -> None:
        self._fernet = Fernet(encryption_key)
        self._token_endpoint = idp_token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        # WeakValueDictionary: locks are dropped when no caller holds them,
        # preventing unbounded growth across many users.
        self._refresh_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @staticmethod
    def derive_key(secret: str) -> bytes:
        """Derive a Fernet-compatible key from an arbitrary secret string."""
        if not secret:
            raise ValueError("derive_key requires a non-empty secret")
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def _get_lock(self, user_id: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[user_id] = lock
        return lock

    def _encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def _decrypt(self, ciphertext: bytes) -> str:
        return self._fernet.decrypt(ciphertext).decode("utf-8")

    async def store_initial(
        self,
        user_id: str,
        refresh_token: str,
        access_token: str | None,
        expires_in: int | None,
    ) -> None:
        """Persist tokens after a successful OIDC code exchange."""
        from rolemesh.db import upsert_user_oidc_tokens

        refresh_enc = self._encrypt(refresh_token)
        access_enc = self._encrypt(access_token) if access_token else None
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in)
            if access_token and expires_in
            else None
        )
        await upsert_user_oidc_tokens(user_id, refresh_enc, access_enc, expires_at)
        logger.info("OIDC tokens stored", user_id=user_id)

    async def get_fresh_access_token(self, user_id: str) -> str | None:
        """Return a valid access_token for the user, refreshing if needed.

        Returns None if no tokens are stored or the refresh failed.
        """
        from rolemesh.db import (
            delete_user_oidc_tokens,
            get_user_oidc_tokens,
            update_user_access_token,
            update_user_refresh_token,
        )

        lock = self._get_lock(user_id)
        async with lock:
            row = await get_user_oidc_tokens(user_id)
            if row is None:
                return None
            refresh_enc, access_enc, expires_at = row

            # Cached access_token still valid?
            if access_enc and expires_at:
                seconds_left = (expires_at - datetime.now(UTC)).total_seconds()
                if seconds_left > _REFRESH_THRESHOLD_SECONDS:
                    try:
                        return self._decrypt(access_enc)
                    except InvalidToken:
                        logger.warning("Failed to decrypt cached access_token", user_id=user_id)
                        # Fall through to refresh

            # Need to refresh
            try:
                refresh_token = self._decrypt(refresh_enc)
            except InvalidToken:
                logger.error("Failed to decrypt refresh_token", user_id=user_id)
                await delete_user_oidc_tokens(user_id)
                return None

            new_access, new_refresh, new_expires_at = await self._call_refresh(refresh_token)
            if new_access is None:
                # IdP rejected refresh — purge stored tokens
                logger.warning("OIDC refresh failed, purging tokens", user_id=user_id)
                await delete_user_oidc_tokens(user_id)
                return None

            # Persist updated access_token
            new_access_enc = self._encrypt(new_access)
            await update_user_access_token(user_id, new_access_enc, new_expires_at)

            # If IdP rotated refresh_token, persist it
            if new_refresh and new_refresh != refresh_token:
                await update_user_refresh_token(user_id, self._encrypt(new_refresh))

            return new_access

    async def revoke(self, user_id: str) -> None:
        """Delete the user's stored tokens (called on logout).

        Lock is not explicitly removed; WeakValueDictionary GCs it when no
        coroutine holds a reference.
        """
        from rolemesh.db import delete_user_oidc_tokens

        await delete_user_oidc_tokens(user_id)

    async def _call_refresh(
        self, refresh_token: str
    ) -> tuple[str | None, str | None, datetime | None]:
        """POST to IdP token endpoint with grant_type=refresh_token.

        Returns (new_access_token, new_refresh_token, expires_at).
        Returns (None, None, None) on failure.
        """
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            data["client_secret"] = self._client_secret

        t_start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._token_endpoint,
                    data=data,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            logger.error("TokenVault network error", error=str(exc), elapsed_ms=elapsed_ms)
            return None, None, None

        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        if resp.status_code != 200:
            logger.warning(
                "TokenVault refresh rejected",
                status=resp.status_code,
                body=resp.text[:200],
                elapsed_ms=elapsed_ms,
            )
            return None, None, None

        payload = resp.json()
        access_token = payload.get("access_token")
        if not access_token:
            return None, None, None
        expires_in = payload.get("expires_in", 3600)
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        # Warn if refresh is unusually slow (sign of IdP latency / network issue)
        if elapsed_ms > 2000:
            logger.warning("TokenVault refresh slow", elapsed_ms=elapsed_ms)
        else:
            logger.debug("TokenVault refresh ok", elapsed_ms=elapsed_ms)
        return access_token, payload.get("refresh_token"), expires_at


# ---------------------------------------------------------------------------
# Factory: build vault from environment (called by both processes)
# ---------------------------------------------------------------------------


async def create_vault_from_env() -> TokenVault | None:
    """Build a TokenVault from OIDC env vars + discovery, or None if not configured.

    Returns None when:
      - AUTH_MODE != "oidc"
      - ROLEMESH_TOKEN_SECRET unset
      - OIDC_DISCOVERY_URL or OIDC_CLIENT_ID unset

    Both the orchestrator process and the WebUI process must call this at
    startup to populate their respective module-level vault references.
    """
    if os.environ.get("AUTH_MODE", "external") != "oidc":
        return None
    secret = os.environ.get("ROLEMESH_TOKEN_SECRET", "")
    if not secret:
        return None
    discovery_url = os.environ.get("OIDC_DISCOVERY_URL", "")
    client_id = os.environ.get("OIDC_CLIENT_ID", "")
    if not discovery_url or not client_id:
        return None
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "")

    # Resolve token endpoint via OIDC discovery
    from rolemesh.auth.oidc.config import OIDCConfig
    from rolemesh.auth.oidc.provider import OIDCAuthProvider

    temp_provider = OIDCAuthProvider(
        OIDCConfig(discovery_url=discovery_url, client_id=client_id),
    )
    disc = await temp_provider.get_discovery()
    return TokenVault(
        encryption_key=TokenVault.derive_key(secret),
        idp_token_endpoint=disc.token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
    )
