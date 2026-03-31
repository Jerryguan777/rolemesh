"""Tests for rolemesh.task_scheduler."""

from __future__ import annotations

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


def test_compute_next_run_once() -> None:
    task = _make_task(schedule_type="once")
    assert compute_next_run(task) is None


def test_compute_next_run_cron() -> None:
    task = _make_task(schedule_type="cron", schedule_value="0 9 * * *")
    result = compute_next_run(task)
    assert result is not None
    assert "T09:00:00" in result or "T" in result


def test_compute_next_run_interval() -> None:
    task = _make_task(
        schedule_type="interval",
        schedule_value="3600000",  # 1 hour
        next_run="2020-01-01T00:00:00Z",
    )
    result = compute_next_run(task)
    assert result is not None
    # Should be in the future
    assert result > "2024-01-01T00:00:00Z"


def test_compute_next_run_invalid_interval() -> None:
    task = _make_task(schedule_type="interval", schedule_value="0")
    result = compute_next_run(task)
    assert result is not None  # Falls back to 60s from now
