"""Shared pytest fixtures for RoleMesh tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from testcontainers.postgres import PostgresContainer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator
    from pathlib import Path


@pytest.fixture(scope="session")
def pg_url() -> Generator[str, None, None]:
    """Start a PostgreSQL container for the test session.

    Durability is turned off (fsync / synchronous_commit / full_page_writes).
    This is a throwaway container — nothing survives the session, so there is
    nothing to make crash-safe — and the fsync per commit/TRUNCATE otherwise
    dominates runtime: it makes the per-test TRUNCATE ~90x slower (≈1.7s → 20ms)
    and adds an fsync to every INSERT the tests do.
    """
    with PostgresContainer("postgres:16").with_command(
        "postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    ) as pg:
        url = pg.get_connection_url()
        # testcontainers returns psycopg2 URL; convert to asyncpg format
        url = url.replace("psycopg2", "postgresql").replace("postgresql+postgresql", "postgresql")
        yield url


@pytest.fixture
async def test_db(pg_url: str) -> AsyncGenerator[None, None]:
    """Give each test a clean database.

    Uses ``_setup_test_database``, which builds the schema once for the
    session's container and TRUNCATEs between tests rather than dropping
    and recreating ~130 DDL objects every test — the latter made per-test
    setup ~4s (the tests themselves run in tens of ms). Pools are still
    opened/closed per test because asyncpg pools are bound to the (function
    scoped) event loop.
    """
    from rolemesh.db import _setup_test_database, close_database

    await _setup_test_database(pg_url)
    yield
    await close_database()


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for test artifacts."""
    return tmp_path


@pytest.fixture
def tmp_env_file(tmp_path: Path) -> Path:
    """Provide a temporary .env file path."""
    return tmp_path / ".env"


@pytest.fixture
def tmp_groups_dir(tmp_path: Path) -> Path:
    """Provide a temporary groups directory."""
    d = tmp_path / "groups"
    d.mkdir()
    return d


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Provide a temporary data directory."""
    d = tmp_path / "data"
    d.mkdir()
    return d
