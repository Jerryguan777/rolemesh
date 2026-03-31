"""Timezone utilities."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def format_local_time(utc_iso: str, tz_name: str) -> str:
    """Convert a UTC ISO timestamp to a localized display string."""
    dt = datetime.fromisoformat(utc_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    return local_dt.strftime("%b %-d, %Y, %-I:%M %p")
