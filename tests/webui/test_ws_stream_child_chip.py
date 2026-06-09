"""Frontdesk v1.5 — ws_stream child-chip projection (`_build_child_chip_frame_or_none`).

The orchestrator publishes delegation child-progress on the PARENT
conversation's ``web.stream.*`` carrier as a ``kind="status"`` chunk whose
inner payload is tagged ``kind="child_chip"``. The v1 ws_stream must project
those four phases into the four ``event.delegation.*`` frames — and must NOT
mistake a ``phase="status"`` chip (which carries a ``status`` field) for a
per-turn ``event.run.progress``.

Invariant under test: every frame the projection emits is a valid
``WsServerEventModel`` member (the wire contract). The projection and the
Pydantic schema are independent code paths, so round-tripping the output
through the model catches drift in either direction.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from webui.schemas_v1 import WsServerEventModel
from webui.v1.ws_stream import (
    _build_child_chip_frame_or_none,
    _build_progress_frame_or_none,
)

_MODEL = TypeAdapter(WsServerEventModel)

_RUN = "11111111-1111-1111-1111-111111111111"
_CHILD = "22222222-2222-2222-2222-222222222222"
_DELEG = "33333333-3333-3333-3333-333333333333"


def _inner(phase: str, **extra: object) -> dict[str, object]:
    """A well-formed child_chip carrier payload for ``phase``."""
    base: dict[str, object] = {
        "kind": "child_chip",
        "phase": phase,
        "child_conv_id": _CHILD,
        "delegation_id": _DELEG,
        "target_folder": "trading",
        "target_name": "Trading Desk",
    }
    base.update(extra)
    return base


def _assert_valid_wire_frame(frame: dict[str, object]) -> None:
    """The frame must validate as a WsServerEvent member (extra=forbid)."""
    _MODEL.validate_python(frame)


# ---------------------------------------------------------------------------
# Phase → frame type, plus contract round-trip
# ---------------------------------------------------------------------------


def test_open_phase_maps_to_started_frame() -> None:
    frame = _build_child_chip_frame_or_none(
        _RUN, _inner("open", context_mode="sticky", initial_status="queued")
    )
    assert frame is not None
    assert frame["type"] == "event.delegation.started"
    assert frame["run_id"] == _RUN
    assert frame["child_conv_id"] == _CHILD
    assert frame["context_mode"] == "sticky"
    assert frame["initial_status"] == "queued"
    _assert_valid_wire_frame(frame)


def test_status_phase_maps_to_delegation_progress_not_run_progress() -> None:
    """The regression this whole interception exists for.

    A child_chip ``phase="status"`` carries a top-level ``status`` field.
    If it ever fell through to ``_build_progress_frame_or_none`` it would be
    emitted as ``event.run.progress`` stamped with the PARENT run_id —
    making the frontdesk's own status bar flicker with the specialist's
    container phase. The projection must claim it first.
    """
    inner = _inner("status", status="running")
    chip = _build_child_chip_frame_or_none(_RUN, inner)
    assert chip is not None
    assert chip["type"] == "event.delegation.progress"
    assert chip["status"] == "running"
    _assert_valid_wire_frame(chip)

    # And prove the danger is real: the progress builder, fed the same
    # payload, WOULD have produced a run-progress frame. The caller relies
    # on the kind=="child_chip" guard to never reach it.
    leaked = _build_progress_frame_or_none(_RUN, inner)
    assert leaked is not None
    assert leaked["type"] == "event.run.progress"


def test_tool_use_phase_renames_tool_input_to_preview() -> None:
    frame = _build_child_chip_frame_or_none(
        _RUN, _inner("tool_use", tool_name="Read", tool_input="path/to/file")
    )
    assert frame is not None
    assert frame["type"] == "event.delegation.tool_use"
    assert frame["tool_name"] == "Read"
    # Renamed at the boundary — the wire field carries the truncation
    # semantic; the raw ``tool_input`` key must not survive.
    assert frame["tool_input_preview"] == "path/to/file"
    assert "tool_input" not in frame
    _assert_valid_wire_frame(frame)


def test_tool_use_null_tool_name_is_preserved_as_none() -> None:
    frame = _build_child_chip_frame_or_none(
        _RUN, _inner("tool_use", tool_name=None)
    )
    assert frame is not None
    assert frame["tool_name"] is None
    assert "tool_input_preview" not in frame
    _assert_valid_wire_frame(frame)


def test_close_phase_maps_to_completed_with_duration() -> None:
    frame = _build_child_chip_frame_or_none(
        _RUN, _inner("close", final_status="success", duration_ms=1234)
    )
    assert frame is not None
    assert frame["type"] == "event.delegation.completed"
    assert frame["final_status"] == "success"
    assert frame["duration_ms"] == 1234
    _assert_valid_wire_frame(frame)


def test_close_without_duration_omits_the_field() -> None:
    frame = _build_child_chip_frame_or_none(
        _RUN, _inner("close", final_status="error")
    )
    assert frame is not None
    assert frame["final_status"] == "error"
    assert "duration_ms" not in frame
    _assert_valid_wire_frame(frame)


# ---------------------------------------------------------------------------
# Whitelist posture + defensive None paths
# ---------------------------------------------------------------------------


def test_unexpected_inner_key_is_not_leaked_to_the_frame() -> None:
    """A future orchestrator-side key (e.g. prompt_sha256) must never reach
    the browser. The frame is built from an explicit field whitelist, and
    the Pydantic model is ``extra="forbid"`` as a second wall."""
    frame = _build_child_chip_frame_or_none(
        _RUN,
        _inner("status", status="running", prompt_sha256="deadbeef", secret="x"),
    )
    assert frame is not None
    assert "prompt_sha256" not in frame
    assert "secret" not in frame
    _assert_valid_wire_frame(frame)


@pytest.mark.parametrize(
    "missing", ["child_conv_id", "delegation_id", "target_folder", "target_name"]
)
def test_missing_identity_field_drops_the_chip(missing: str) -> None:
    inner = _inner("open")
    del inner[missing]
    assert _build_child_chip_frame_or_none(_RUN, inner) is None


def test_status_phase_without_status_is_dropped() -> None:
    assert _build_child_chip_frame_or_none(_RUN, _inner("status")) is None


def test_close_phase_without_final_status_is_dropped() -> None:
    assert _build_child_chip_frame_or_none(_RUN, _inner("close")) is None


def test_unknown_phase_degrades_to_none() -> None:
    assert _build_child_chip_frame_or_none(_RUN, _inner("teleport")) is None


def test_empty_string_identity_is_rejected() -> None:
    """An empty target_name is not a usable chip key/label — reject like a
    missing field rather than emitting a blank chip."""
    assert _build_child_chip_frame_or_none(_RUN, _inner("open", target_name="")) is None
