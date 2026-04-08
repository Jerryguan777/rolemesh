"""AuthProvider-based authentication for web channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

    from rolemesh.auth.provider import AuthenticatedUser, AuthProvider

_pool: asyncpg.Pool[asyncpg.Record] | None = None
_provider: AuthProvider | None = None


async def init_auth(pool: asyncpg.Pool[asyncpg.Record]) -> None:
    """Set the shared database pool."""
    global _pool
    _pool = pool


async def init_auth_provider(mode: str = "") -> None:
    """Initialize the AuthProvider for user-level authentication."""
    global _provider
    try:
        from rolemesh.auth.factory import create_auth_provider

        _provider = create_auth_provider(mode)
    except (ImportError, ValueError):
        _provider = None


def get_provider() -> AuthProvider | None:
    """Return the configured AuthProvider, if any."""
    return _provider


async def authenticate_request(token: str) -> AuthenticatedUser | None:
    """Authenticate a request token via the configured AuthProvider."""
    if _provider is not None:
        return await _provider.authenticate(token)
    return None


BOOTSTRAP_USER_ID = "bootstrap"


async def authenticate_ws(token: str) -> AuthenticatedUser | None:
    """Authenticate a token (JWT or bootstrap). Returns None on failure.

    Used by both WebSocket and REST endpoints.
    """
    from rolemesh.auth.provider import AuthenticatedUser as AuthUser
    from webui.config import ADMIN_BOOTSTRAP_TOKEN

    if ADMIN_BOOTSTRAP_TOKEN and token == ADMIN_BOOTSTRAP_TOKEN:
        from rolemesh.db.pg import get_tenant_by_slug

        tenant = await get_tenant_by_slug("default")
        tenant_id = tenant.id if tenant else "default"
        return AuthUser(
            user_id=BOOTSTRAP_USER_ID,
            tenant_id=tenant_id,
            role="owner",
            name="Bootstrap Admin",
        )
    return await authenticate_request(token)


def get_pool() -> asyncpg.Pool | None:  # type: ignore[type-arg]
    """Return the database pool (for read-only queries)."""
    return _pool


async def close_auth() -> None:
    """Release reference to the shared pool. The pool itself is closed by pg.close_database()."""
    global _pool
    _pool = None
