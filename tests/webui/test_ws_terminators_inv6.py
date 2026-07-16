"""INV-6 single-writer pin: the WS handler must never write the runs table.

History: 01c smoke found ``terminate_run_via_ws_completed`` defined but
never called, so runs stayed at ``running``; the first fix put the
terminal writer INTO ``webui.v1.ws_stream`` (this file's original
subject). That created dual writers — orchestrator + WS — racing on the
``WHERE status='running'`` guard, and the ``done`` stream chunk was
forced to carry two meanings at once (bubble finished vs run finished).
Field trace: a content-filter kill mid-generation left the row
``failed`` while the WS still framed ``event.run.completed`` — the
single-writer refactor removed that race at its root.

Now the orchestrator is the ONLY terminal writer (INV-6 paths 1/2 live
in ``rolemesh.main._terminate_run_safe``; DB-backed coverage in
``tests/orchestration/test_run_terminal_single_writer.py``). The WS
handler is a pure projection:

* ``done``            → ``event.run.output_done``  (bubble terminator)
* ``run_completed``   → ``event.run.completed``    (run terminal)
* ``run_error``       → ``event.run.error``        (run terminal)
* ``safety_blocked``  → ``event.run.error``        (frame only)

These tests pin (a) the projection function's wire contract and (b) a
source-level guard that ws_stream cannot regrow a runs-table writer.
"""

from __future__ import annotations

import inspect
import json

from webui.v1 import ws_stream
from webui.v1.ws_stream import _run_terminal_frame_or_none

# ---------------------------------------------------------------------------
# Single-writer guard
# ---------------------------------------------------------------------------


def test_ws_stream_has_no_runs_table_writer() -> None:
    """Source-level pin: the WS module must not import or call any runs
    terminator. If someone re-adds a ``terminate_run_via_*`` call here,
    the dual-writer race (two writers deciding one row, frames emitted
    from the loser's perspective) comes back — fail loudly.

    ``create_run`` (request.run mints the row) is the ONE allowed runs
    write; everything terminal belongs to the orchestrator.
    """
    src = inspect.getsource(ws_stream)
    assert "terminate_run_via" not in src, (
        "ws_stream must stay a pure projection — terminal writes live in "
        "rolemesh.main (single-writer contract)"
    )
    assert "update_run_terminal" not in src


# ---------------------------------------------------------------------------
# Projection: run_completed / run_error chunks → frames
# ---------------------------------------------------------------------------


def test_run_completed_chunk_projects_completed_frame() -> None:
    frame = _run_terminal_frame_or_none(
        "run_completed", "run-closure", json.dumps({"run_id": "run-echo"})
    )
    assert frame == {"type": "event.run.completed", "run_id": "run-echo"}


def test_terminal_chunk_run_id_wins_over_closure() -> None:
    """The chunk's run_id is container-attributed (container echo) and
    authoritative; the closure's active_run_id goes stale on
    warm-container follow-ups and is only the fallback."""
    frame = _run_terminal_frame_or_none(
        "run_error",
        "run-closure",
        json.dumps({"run_id": "run-echo", "error": {"code": "X", "message": "m"}}),
    )
    assert frame is not None
    assert frame["run_id"] == "run-echo"


def test_terminal_chunk_without_run_id_falls_back_to_closure() -> None:
    frame = _run_terminal_frame_or_none("run_completed", "run-closure", "{}")
    assert frame == {"type": "event.run.completed", "run_id": "run-closure"}


def test_run_error_chunk_projects_error_frame_with_details() -> None:
    error = {
        "code": "SAFETY_BLOCKED",
        "message": "policy violation",
        "stage": "input_prompt",
        "rule_id": "rule-42",
    }
    frame = _run_terminal_frame_or_none(
        "run_error", "run-1", json.dumps({"run_id": "run-1", "error": error})
    )
    assert frame == {
        "type": "event.run.error",
        "run_id": "run-1",
        "code": "SAFETY_BLOCKED",
        "message": "policy violation",
        "details": error,
    }


def test_run_error_chunk_with_junk_error_uses_defaults() -> None:
    """A malformed error payload must not crash the forwarder or leak
    raw junk — conservative defaults instead."""
    frame = _run_terminal_frame_or_none(
        "run_error", "run-1", json.dumps({"run_id": "run-1", "error": "not-a-dict"})
    )
    assert frame == {
        "type": "event.run.error",
        "run_id": "run-1",
        "code": "AGENT_ERROR",
        "message": "run failed",
        "details": {},
    }


def test_terminal_chunk_with_non_dict_content_still_frames() -> None:
    frame = _run_terminal_frame_or_none("run_completed", "run-1", json.dumps([1, 2]))
    assert frame == {"type": "event.run.completed", "run_id": "run-1"}
