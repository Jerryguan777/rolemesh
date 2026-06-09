"""AuthProvider-based authentication for web channel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rolemesh.auth.bootstrap_users import (
    ensure_bootstrap_user_row,
    get_spec_for_token,
)

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


async def authenticate_ws(token: str) -> AuthenticatedUser | None:
    """Authenticate a token (BOOTSTRAP_USERS or configured provider).

    Returns None on failure. Used by both WebSocket and REST endpoints.

    Resolution order:
      1. ``BOOTSTRAP_USERS`` multi-user map (if any token matches);
      2. configured AuthProvider (external JWT / OIDC / builtin).

    A request whose token matches no BOOTSTRAP_USERS spec falls through
    to the provider — never short-circuited. Every path returns a
    user whose ``user_id`` is a real UUID (BOOTSTRAP_USERS upserts a
    row; the external/OIDC providers enforce the same invariant).
    """
    from rolemesh.auth.provider import AuthenticatedUser as AuthUser
    from rolemesh.db import get_tenant_by_slug

    spec = get_spec_for_token(token)
    if spec is not None:
        tenant = await get_tenant_by_slug(spec.tenant_slug)
        if tenant is None:
            # Spec referenced a tenant slug that isn't in the DB. Fail
            # closed: don't manufacture a fictitious tenant_id.
            return None
        user_uuid = await ensure_bootstrap_user_row(spec, tenant.id)
        return AuthUser(
            user_id=user_uuid,
            tenant_id=tenant.id,
            role=spec.role,
            name=spec.user_id_slug,
        )

    return await authenticate_request(token)


def get_pool() -> asyncpg.Pool | None:  # type: ignore[type-arg]
    """Return the database pool (for read-only queries)."""
    return _pool


async def close_auth() -> None:
    """Release reference to the shared pool. The pool itself is closed by pg.close_database()."""
    global _pool
    _pool = None
