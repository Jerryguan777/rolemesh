"""Tests for rolemesh.timezone."""

from rolemesh.core.timezone import format_local_time


def test_format_local_time_utc() -> None:
    result = format_local_time("2024-01-15T14:30:00Z", "UTC")
    assert "Jan" in result
    assert "15" in result
    assert "2024" in result
    assert "2:30 PM" in result


def test_format_local_time_with_timezone() -> None:
    result = format_local_time("2024-01-15T14:30:00Z", "America/New_York")
    assert "Jan" in result
    assert "15" in result
    assert "9:30 AM" in result
