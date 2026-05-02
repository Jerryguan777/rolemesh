"""PostgreSQL connection pools and database lifecycle.

Holds the module-level ``_pool`` (NOBYPASSRLS business pool) and
``_admin_pool`` (BYPASSRLS maintenance pool). Every CRUD module imports
``tenant_conn`` / ``admin_conn`` from here. Lifecycle helpers
(``init_database`` / ``close_database`` / ``_init_test_database``)
live here too because they are the only writers of the pool globals.

DDL is owned by ``rolemesh.db.schema`` — pure DDL, no pool dependency.
``init_database`` invokes ``_create_schema(conn)`` on the admin pool
once both pools are open.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg

from rolemesh.core.config import ADMIN_DATABASE_URL, DATABASE_URL
from rolemesh.core.logger import get_logger
from rolemesh.db.schema import _create_schema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger()


__all__ = [
    "DEFAULT_TENANT",
    "_get_admin_pool",
    "_get_pool",
    "_init_test_database",
    "admin_conn",
    "close_database",
    "init_database",
    "tenant_conn",
]


def _to_dt(ts: str | None) -> datetime | None:
    """Convert an ISO timestamp string to a datetime object for asyncpg."""
    if not ts:
        return None
    return datetime.fromisoformat(ts)


_pool: asyncpg.Pool[asyncpg.Record] | None = None
_admin_pool: asyncpg.Pool[asyncpg.Record] | None = None
DEFAULT_TENANT: str = "default"


def _get_pool() -> asyncpg.Pool[asyncpg.Record]:
    """Return the module-level connection pool, asserting it is initialized."""
    assert _pool is not None, "Database not initialized. Call await init_database() first."
    return _pool


def _get_admin_pool() -> asyncpg.Pool[asyncpg.Record]:
    """Return the BYPASSRLS admin pool used by cross-tenant maintenance,
    resolvers, and DDL.

    Asserts the pool was initialised. The previous version silently
    fell back to the business pool when ``_admin_pool`` was None so
    dev/test wouldn't have to set ``ADMIN_DATABASE_URL`` — but that
    fallback also masks a real misconfiguration in prod (admin path
    suddenly running under ``rolemesh_app`` NOBYPASSRLS, every
    cross-tenant query returns zero rows). Both ``init_database``
    and ``_init_test_database`` already create ``_admin_pool``
    unconditionally (with a DSN fallback to ``DATABASE_URL`` when
    ``ADMIN_DATABASE_URL`` is unset), so dev convenience is
    preserved without the silent-fallback hazard.
    """
    assert _admin_pool is not None, (
        "Admin pool not initialised. Call await init_database() first."
    )
    return _admin_pool


@asynccontextmanager
async def tenant_conn(
    tenant_id: str,
) -> AsyncIterator[asyncpg.pool.PoolConnectionProxy[asyncpg.Record]]:
    """Acquire a business connection bound to ``tenant_id``.

    Sets ``app.current_tenant_id`` GUC inside an explicit transaction so
    the value is wiped when the transaction commits/rolls back. RLS
    policies (added in PR-D) read it via ``current_tenant_id()``.

    Use ``set_config(name, value, is_local=true)`` rather than
    ``SET LOCAL`` so the value can be parameterised — string-concat'd
    SET LOCAL would be a SQL injection foothold.
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(tenant_id),
        )
        yield conn


@asynccontextmanager
async def admin_conn() -> AsyncIterator[asyncpg.pool.PoolConnectionProxy[asyncpg.Record]]:
    """Acquire a BYPASSRLS connection for cross-tenant maintenance.

    Callers must justify cross-tenant scope in their docstring (see the
    classification in the RLS design: B = list_*/cleanup_*, C =
    resolve_*, D = init_*/_create_*). Never use this from REST handlers.
    """
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        yield conn


async def init_database(
    database_url: str | None = None,
    admin_database_url: str | None = None,
) -> None:
    """Initialize PostgreSQL pools and create schema.

    ``database_url`` connects the business pool (drops to ``rolemesh_app``
    in production after PR-D). ``admin_database_url`` connects the
    BYPASSRLS pool used by maintenance/resolvers; if omitted, falls
    back to ``ADMIN_DATABASE_URL`` env, then to ``database_url``.
    """
    global _pool, _admin_pool
    url = database_url or DATABASE_URL
    admin_url = admin_database_url or ADMIN_DATABASE_URL or url
    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    _admin_pool = await asyncpg.create_pool(admin_url, min_size=1, max_size=3)
    async with _admin_pool.acquire() as conn:
        await _create_schema(conn)
    await _log_pool_identities()


async def _log_pool_identities() -> None:
    """Emit one structured log line per pool with its current_user.

    Lets operators verify at boot that the business pool is connected
    as the NOBYPASSRLS role and the admin pool as the BYPASSRLS role
    (or that they share an identity in dev/test, which is also useful
    to see explicitly).
    """
    if _pool is not None:
        async with _pool.acquire() as conn:
            who = await conn.fetchval("SELECT current_user")
        logger.info("DB business pool ready", current_user=who)
    if _admin_pool is not None:
        async with _admin_pool.acquire() as conn:
            who = await conn.fetchval("SELECT current_user")
        logger.info("DB admin pool ready", current_user=who)


async def _init_test_database(
    database_url: str, admin_database_url: str | None = None
) -> None:
    """Initialize a test database with a fresh schema."""
    global _pool, _admin_pool
    admin_url = admin_database_url or database_url
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    _admin_pool = await asyncpg.create_pool(admin_url, min_size=1, max_size=3)
    async with _admin_pool.acquire() as conn:
        # Drop all tables for a clean slate
        await conn.execute("DROP TABLE IF EXISTS eval_runs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS safety_decisions CASCADE")
        await conn.execute("DROP TABLE IF EXISTS safety_rules_audit CASCADE")
        await conn.execute("DROP TABLE IF EXISTS safety_rules CASCADE")
        await conn.execute("DROP TABLE IF EXISTS approval_audit_log CASCADE")
        await conn.execute("DROP TABLE IF EXISTS approval_requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS approval_policies CASCADE")
        await conn.execute("DROP TABLE IF EXISTS oidc_user_tokens CASCADE")
        await conn.execute("DROP TABLE IF EXISTS external_tenant_map CASCADE")
        await conn.execute("DROP TABLE IF EXISTS skill_files CASCADE")
        await conn.execute("DROP TABLE IF EXISTS skills CASCADE")
        await conn.execute("DROP TABLE IF EXISTS user_agent_assignments CASCADE")
        await conn.execute("DROP TABLE IF EXISTS task_run_logs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS scheduled_tasks CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions_legacy CASCADE")
        await conn.execute("DROP TABLE IF EXISTS conversations CASCADE")
        await conn.execute("DROP TABLE IF EXISTS channel_bindings CASCADE")
        await conn.execute("DROP TABLE IF EXISTS coworkers CASCADE")
        await conn.execute("DROP TABLE IF EXISTS roles CASCADE")
        await conn.execute("DROP TABLE IF EXISTS users CASCADE")
        await conn.execute("DROP TABLE IF EXISTS tenants CASCADE")
        await conn.execute("DROP TABLE IF EXISTS chats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS registered_groups CASCADE")
        await conn.execute("DROP TABLE IF EXISTS router_state CASCADE")
        await _create_schema(conn)


async def close_database() -> None:
    """Close both connection pools. Call on shutdown."""
    global _pool, _admin_pool
    if _pool:
        await _pool.close()
        _pool = None
    if _admin_pool:
        await _admin_pool.close()
        _admin_pool = None
