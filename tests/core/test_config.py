"""Tests for rolemesh.config."""

import importlib

import pytest

import rolemesh.core.config as config_module
from rolemesh.core.config import (
    APPROVAL_TIMEOUT,
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


# ---------------------------------------------------------------------------
# HITL approval (docs/21-hitl-approval-plan.md §5)
# ---------------------------------------------------------------------------


def test_approval_timeout_default_is_5_min() -> None:
    assert APPROVAL_TIMEOUT == 300_000


def test_approval_timeout_below_watchdog_floor() -> None:
    # The frozen §5 invariant: the approval await must fire before the
    # container watchdog floor (IDLE_TIMEOUT + 30_000) so the watchdog can
    # never pre-empt a pending approval.
    assert APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30_000


def test_startup_assertion_refuses_to_start_when_timeout_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A misconfigured deployment whose APPROVAL_TIMEOUT would let the
    # watchdog reap a pending approval must refuse to start, not silently
    # kill containers mid-approval. We reload the module under a bad env to
    # exercise the module-level guard, then reload clean to restore state.
    monkeypatch.setenv("IDLE_TIMEOUT", "1800000")
    monkeypatch.setenv("APPROVAL_TIMEOUT", str(1_800_000 + 30_000))  # == floor, not <
    try:
        with pytest.raises(ValueError, match="APPROVAL_TIMEOUT"):
            importlib.reload(config_module)
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
