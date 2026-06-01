"""Tests for compute_next_run (cron / interval / once scheduling).

Focus on the edges that bite in production: malformed schedule values,
a next_run far in the past (the rollforward loop), and the precondition
asymmetry between the cron and interval branches.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from rolemesh.core.types import ScheduledTask
from rolemesh.orchestration.task_scheduler import compute_next_run


def _make_task(
    schedule_type: str = "cron",
    schedule_value: str = "0 9 * * *",
    next_run: str | None = "2024-01-01T09:00:00Z",
) -> ScheduledTask:
    return ScheduledTask(
        id="t1",
        tenant_id="tenant-1",
        coworker_id="cw-1",
        prompt="test",
        schedule_type=schedule_type,  # type: ignore[arg-type]
        schedule_value=schedule_value,
        context_mode="isolated",
        next_run=next_run,
        status="active",
        created_at="2024-01-01T00:00:00Z",
    )


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _now() -> datetime:
    return datetime.now(UTC)


# --- once --------------------------------------------------------------------


def test_once_never_reschedules() -> None:
    assert compute_next_run(_make_task(schedule_type="once")) is None


# --- cron --------------------------------------------------------------------


def test_cron_returns_next_matching_instant_in_the_future() -> None:
    """0 9 * * * → the next 09:00 UTC, strictly after now. Asserts the
    actual computed time, not just that the string contains a 'T'."""
    result = compute_next_run(_make_task(schedule_type="cron", schedule_value="0 9 * * *"))
    assert result is not None
    dt = _parse(result)
    assert dt > _now()
    assert (dt.hour, dt.minute, dt.second) == (9, 0, 0)


def test_cron_every_minute_is_within_one_minute() -> None:
    result = compute_next_run(_make_task(schedule_type="cron", schedule_value="* * * * *"))
    assert result is not None
    delta = (_parse(result) - _now()).total_seconds()
    assert 0 < delta <= 60


def test_cron_invalid_expression_raises() -> None:
    """The cron branch has no fallback (unlike interval). A malformed cron
    propagates as a ValueError — pin that so the asymmetry is a known,
    tested contract rather than a silent surprise."""
    with pytest.raises(ValueError):
        compute_next_run(_make_task(schedule_type="cron", schedule_value="not a cron"))


# --- interval ----------------------------------------------------------------


def test_interval_advances_by_one_period_from_recent_next_run() -> None:
    """next_run just under one period ago → result lands in (now, now+period]."""
    period_ms = 3600_000  # 1h
    base = (_now().timestamp()) - 1800  # 30 min ago
    task = _make_task(
        schedule_type="interval",
        schedule_value=str(period_ms),
        next_run=datetime.fromtimestamp(base, tz=UTC).isoformat(),
    )
    result = compute_next_run(task)
    assert result is not None
    delta = (_parse(result) - _now()).total_seconds()
    assert 0 < delta <= period_ms / 1000


def test_interval_rolls_forward_past_a_stale_next_run() -> None:
    """A next_run years in the past must still resolve to a future instant
    inside one period — the while-loop rolls it forward. Uses a 1-day
    period so the many iterations stay cheap (the O(n) roll-forward is a
    known cost; tiny periods + ancient next_run would be pathological)."""
    period_ms = 86_400_000  # 1 day
    task = _make_task(
        schedule_type="interval",
        schedule_value=str(period_ms),
        next_run="2000-01-01T00:00:00+00:00",
    )
    result = compute_next_run(task)
    assert result is not None
    dt = _parse(result)
    now = _now()
    assert dt > now
    assert (dt - now).total_seconds() <= period_ms / 1000


def test_interval_zero_falls_back_to_one_minute_from_now() -> None:
    result = compute_next_run(_make_task(schedule_type="interval", schedule_value="0"))
    assert result is not None
    delta = (_parse(result) - _now()).total_seconds()
    assert 55 <= delta <= 65


def test_interval_non_numeric_falls_back_to_one_minute() -> None:
    """A non-numeric interval (corrupt row, bad API input) must not crash —
    it parses to 0 and uses the safe 60s fallback."""
    result = compute_next_run(_make_task(schedule_type="interval", schedule_value="abc"))
    assert result is not None
    delta = (_parse(result) - _now()).total_seconds()
    assert 55 <= delta <= 65


def test_interval_negative_falls_back_to_one_minute() -> None:
    result = compute_next_run(_make_task(schedule_type="interval", schedule_value="-5000"))
    assert result is not None
    delta = (_parse(result) - _now()).total_seconds()
    assert 55 <= delta <= 65


def test_interval_with_valid_period_but_null_next_run_raises() -> None:
    """A valid interval needs a next_run anchor. A None anchor hits the
    assert precondition rather than silently scheduling — document the
    sharp edge so callers know to always set next_run for interval tasks."""
    task = _make_task(schedule_type="interval", schedule_value="3600000", next_run=None)
    with pytest.raises(AssertionError):
        compute_next_run(task)


def test_interval_with_malformed_next_run_raises() -> None:
    task = _make_task(schedule_type="interval", schedule_value="3600000", next_run="not-a-date")
    with pytest.raises(ValueError):
        compute_next_run(task)
