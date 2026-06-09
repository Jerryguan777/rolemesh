"""Pure tests for the child-chip throttle bucket + translation.

No DB, no executor, no NATS. Exercises adversarial edge cases that
would surface real bugs if the implementation drifted:

  - first emit must pass (otherwise the UI never shows)
  - second emit within the window must defer (not double-emit)
  - within-window writes must OVERWRITE deferred (last-write-wins —
    otherwise a 10-event burst floods the WS on flush)
  - cross-phase isolation (a running-status emit must not affect
    the tool_use throttle slot)
  - flush_all clears deferred AFTER returning them (idempotent flush)
  - tool_use translation pulls `tool` and `input` from metadata; a
    payload with metadata=None must not crash
  - running / queued / container_starting all collapse to the same
    "status" phase slot so the UI single-line surface stays stable
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from rolemesh.agent.executor import AgentOutput
from rolemesh.orchestration._chip_throttle import (
    _THROTTLE_WINDOW_S,
    ChipThrottleBucket,
)
from rolemesh.orchestration.delegation import _translate_progress_to_chip

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# ChipThrottleBucket
# ---------------------------------------------------------------------------


def test_first_emit_passes_immediately() -> None:
    """The UI must see the very first event of each phase. A deferred-on-first
    bug would make the sub-chip appear blank for 500ms after every
    delegation start — visually mistakable for "the chip is stuck"."""
    bucket = ChipThrottleBucket()
    emit_now, prior = bucket.should_emit("status", {"status": "running"})
    assert emit_now is True
    assert prior is None


def test_second_emit_within_window_defers() -> None:
    bucket = ChipThrottleBucket()
    bucket.should_emit("status", {"status": "running"})
    emit_now, prior = bucket.should_emit(
        "status", {"status": "container_starting"},
    )
    assert emit_now is False
    assert prior is None  # No prior deferred existed; we ARE the deferred now.


def test_within_window_writes_overwrite_deferred() -> None:
    """A burst of tool_use events within the window must collapse to ONE
    deferred payload — the latest. Otherwise flush_all would dump 10
    payloads at once and the UI would render a stutter of stale tool
    names before settling. Regression: an earlier draft kept a list."""
    bucket = ChipThrottleBucket()
    bucket.should_emit("tool_use", {"tool_name": "first"})
    bucket.should_emit("tool_use", {"tool_name": "middle"})
    bucket.should_emit("tool_use", {"tool_name": "latest"})
    flushed = bucket.flush_all()
    assert len(flushed) == 1
    phase, payload = flushed[0]
    assert phase == "tool_use"
    assert payload == {"tool_name": "latest"}


def test_cross_phase_emits_are_independent() -> None:
    bucket = ChipThrottleBucket()
    e1, _ = bucket.should_emit("status", {"status": "running"})
    e2, _ = bucket.should_emit("tool_use", {"tool_name": "bash"})
    assert e1 is True
    assert e2 is True  # Different phase = its own first-emit budget.


def test_emit_after_window_returns_prior_deferred() -> None:
    """After the throttle window elapses, the next emit must release the
    deferred payload SO IT PRECEDES the new emit in time. Otherwise the
    UI sees the stale payload AFTER the new one — frames out of order."""
    bucket = ChipThrottleBucket()
    bucket.should_emit("status", {"status": "running"})           # passes
    bucket.should_emit("status", {"status": "container_starting"})  # deferred
    time.sleep(_THROTTLE_WINDOW_S + 0.05)
    emit_now, prior = bucket.should_emit("status", {"status": "running"})
    assert emit_now is True
    assert prior == {"status": "container_starting"}


def test_flush_all_clears_deferred() -> None:
    """A second flush_all (paranoid double-call from a failing close
    path) must not re-emit the same deferred payload."""
    bucket = ChipThrottleBucket()
    bucket.should_emit("status", {"status": "running"})
    bucket.should_emit("status", {"status": "queued"})  # deferred
    first = bucket.flush_all()
    second = bucket.flush_all()
    assert len(first) == 1
    assert second == []


def test_flush_all_empty_when_no_deferred() -> None:
    bucket = ChipThrottleBucket()
    bucket.should_emit("status", {"status": "running"})
    assert bucket.flush_all() == []


# ---------------------------------------------------------------------------
# _translate_progress_to_chip
# ---------------------------------------------------------------------------


def _record_schedule() -> tuple[
    list[tuple[str, dict[str, Any]]],
    Callable[[str, dict[str, Any]], None],
]:
    """Build a `(events, schedule)` pair to spy on chip scheduling."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _schedule(phase: str, payload: dict[str, Any]) -> None:
        events.append((phase, payload))

    return events, _schedule


def test_translate_tool_use_extracts_metadata() -> None:
    events, schedule = _record_schedule()
    bucket = ChipThrottleBucket()
    out = AgentOutput(
        status="tool_use",
        result=None,
        metadata={"tool": "mcp__rolemesh__send_message", "input": "(...) "},
    )
    _translate_progress_to_chip(out, bucket, schedule)
    assert len(events) == 1
    phase, payload = events[0]
    assert phase == "tool_use"
    assert payload["tool_name"] == "mcp__rolemesh__send_message"
    assert payload["tool_input"] == "(...) "


def test_translate_tool_use_with_missing_metadata_does_not_crash() -> None:
    """A backend that drops metadata (or downgrades it to None) must
    still produce a chip event — just one with nulls, so the UI shows
    a generic "tool" line instead of breaking the whole stream."""
    events, schedule = _record_schedule()
    bucket = ChipThrottleBucket()
    out = AgentOutput(status="tool_use", result=None, metadata=None)
    _translate_progress_to_chip(out, bucket, schedule)
    assert len(events) == 1
    phase, payload = events[0]
    assert phase == "tool_use"
    assert payload["tool_name"] is None
    assert payload["tool_input"] is None


def test_translate_running_emits_status_phase() -> None:
    events, schedule = _record_schedule()
    bucket = ChipThrottleBucket()
    _translate_progress_to_chip(
        AgentOutput(status="running", result=None), bucket, schedule,
    )
    assert events == [("status", {"phase_kind": "status", "status": "running"})]


def test_translate_queued_and_container_starting_share_status_slot() -> None:
    """All three "I'm working" statuses must collapse to the same
    throttle slot — otherwise quick queued→container_starting→running
    transitions would each get their own emit and dominate the WS.
    """
    events, schedule = _record_schedule()
    bucket = ChipThrottleBucket()
    # First emit passes
    _translate_progress_to_chip(
        AgentOutput(status="queued", result=None), bucket, schedule,
    )
    # Second within window must defer
    _translate_progress_to_chip(
        AgentOutput(status="container_starting", result=None), bucket, schedule,
    )
    _translate_progress_to_chip(
        AgentOutput(status="running", result=None), bucket, schedule,
    )
    assert len(events) == 1  # only the first passes through
    assert events[0][1]["status"] == "queued"
    # The latest deferred should be "running" (last-write-wins)
    deferred = bucket.flush_all()
    assert deferred == [("status", {"phase_kind": "status", "status": "running"})]


def test_throttle_blocks_burst_of_tool_use() -> None:
    """20 rapid tool_use events must produce exactly one emit + one
    deferred. Regression guard: if `should_emit` ever started returning
    True on every other call (off-by-one in the timestamp check), a
    burst would emit ~10 events instead of 1."""
    events, schedule = _record_schedule()
    bucket = ChipThrottleBucket()
    for i in range(20):
        out = AgentOutput(
            status="tool_use", result=None, metadata={"tool": f"t{i}"},
        )
        _translate_progress_to_chip(out, bucket, schedule)
    assert len(events) == 1
    # Whichever was first IS the one that passed.
    assert events[0][1]["tool_name"] == "t0"
    # The deferred should be the last one (t19) — last-write-wins.
    deferred = bucket.flush_all()
    assert len(deferred) == 1
    assert deferred[0][1]["tool_name"] == "t19"
