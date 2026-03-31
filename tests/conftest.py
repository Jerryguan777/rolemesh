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
    """Start a PostgreSQL container for the test session."""
    with PostgresContainer("postgres:16") as pg:
        url = pg.get_connection_url()
        # testcontainers returns psycopg2 URL; convert to asyncpg format
        url = url.replace("psycopg2", "postgresql").replace("postgresql+postgresql", "postgresql")
        yield url


@pytest.fixture
async def test_db(pg_url: str) -> AsyncGenerator[None, None]:
    """Initialize a fresh test database for each test."""
    from rolemesh.db.pg import _init_test_database, close_database

    await _init_test_database(pg_url)
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
