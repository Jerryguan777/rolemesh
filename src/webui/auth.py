"""Token validation for web channel bindings and AuthProvider integration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import asyncpg

from webui.config import DATABASE_URL

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser, AuthProvider

# binding_id -> api_token (loaded on startup)
_token_map: dict[str, str] = {}
_pool: asyncpg.Pool | None = None  # type: ignore[type-arg]
_provider: AuthProvider | None = None


async def init_auth() -> None:
    """Connect to the database and load all web-type channel bindings."""
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    await reload_tokens()


async def init_auth_provider(mode: str = "") -> None:
    """Initialize the AuthProvider for user-level authentication."""
    global _provider
    try:
        from rolemesh.auth.factory import create_auth_provider

        _provider = create_auth_provider(mode)
    except (ImportError, ValueError):
        _provider = None


async def authenticate_request(token: str) -> AuthenticatedUser | None:
    """Authenticate a request token via the configured AuthProvider."""
    if _provider is not None:
        return await _provider.authenticate(token)
    return None


async def reload_tokens() -> None:
    """Reload web binding tokens from the database."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, credentials FROM channel_bindings WHERE channel_type = 'web' AND status = 'active'"
        )
    _token_map.clear()
    for row in rows:
        binding_id = str(row["id"])
        creds = row["credentials"]
        if isinstance(creds, str):
            creds = json.loads(creds)
        api_token = creds.get("api_token", "") if creds else ""
        if api_token:
            _token_map[binding_id] = api_token


def validate_token(binding_id: str, token: str) -> bool:
    """Return True if the token matches the binding's api_token."""
    expected = _token_map.get(binding_id)
    if not expected:
        return False
    return expected == token


def get_pool() -> asyncpg.Pool | None:  # type: ignore[type-arg]
    """Return the database pool (for read-only queries)."""
    return _pool


async def close_auth() -> None:
    """Close the database pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
