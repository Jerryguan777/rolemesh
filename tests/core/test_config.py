"""Tests for rolemesh.config."""

from rolemesh.core.config import (
    ASSISTANT_NAME,
    CONTAINER_IMAGE,
    CONTAINER_TIMEOUT,
    CREDENTIAL_PROXY_PORT,
    DATA_DIR,
    GLOBAL_MAX_CONTAINERS,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    MAX_CONCURRENT_CONTAINERS,
    NATS_URL,
    POLL_INTERVAL,
)


def test_default_values() -> None:
    assert isinstance(ASSISTANT_NAME, str)
    assert POLL_INTERVAL == 2.0
    assert CONTAINER_TIMEOUT == 1800000
    assert CREDENTIAL_PROXY_PORT == 3001
    assert IDLE_TIMEOUT == 1800000
    assert MAX_CONCURRENT_CONTAINERS >= 1


def test_paths_exist_as_path_objects() -> None:
    assert hasattr(GROUPS_DIR, "exists")
    assert hasattr(DATA_DIR, "exists")


def test_global_max_containers() -> None:
    assert GLOBAL_MAX_CONTAINERS >= 1
    assert isinstance(GLOBAL_MAX_CONTAINERS, int)


def test_container_image() -> None:
    assert isinstance(CONTAINER_IMAGE, str)
    assert "rolemesh" in CONTAINER_IMAGE


def test_nats_url_default() -> None:
    assert isinstance(NATS_URL, str)
    assert NATS_URL.startswith("nats://")
