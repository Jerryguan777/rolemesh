"""Tests for rolemesh.remote_control."""

from __future__ import annotations

from rolemesh.orchestration.remote_control import (
    _reset_for_testing,
    get_active_session,
    stop_remote_control,
)


def test_no_active_session() -> None:
    _reset_for_testing()
    assert get_active_session() is None


def test_stop_no_session() -> None:
    _reset_for_testing()
    result = stop_remote_control()
    assert result["ok"] is False
    assert "No active" in result.get("error", "")
