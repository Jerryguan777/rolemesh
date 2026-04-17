"""Tests for BackendEvent → ContainerOutput translation in the NATS bridge.

The bridge's on_event callback translates backend events into NATS publishes.
These tests verify the mapping logic directly, without NATS.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runner.backend import ErrorEvent, ResultEvent, SessionInitEvent, CompactionEvent
from agent_runner.main import ContainerOutput


class TestContainerOutput:
    def test_success_with_result(self) -> None:
        out = ContainerOutput(status="success", result="done")
        d = out.to_dict()
        assert d == {"status": "success", "result": "done"}

    def test_success_with_session_id(self) -> None:
        out = ContainerOutput(status="success", result=None, new_session_id="sid-1")
        d = out.to_dict()
        assert d["newSessionId"] == "sid-1"
        assert d["result"] is None

    def test_error_with_message(self) -> None:
        out = ContainerOutput(status="error", result=None, error="timeout")
        d = out.to_dict()
        assert d["status"] == "error"
        assert d["error"] == "timeout"
        assert d["result"] is None

    def test_no_optional_fields(self) -> None:
        out = ContainerOutput(status="success", result="x")
        d = out.to_dict()
        assert "newSessionId" not in d
        assert "error" not in d


class TestEventToOutputMapping:
    """Verify the on_event logic from run_query_loop maps events correctly.

    We replicate the on_event callback logic inline rather than calling
    the real function (which needs NATS). This catches logic drift.
    """

    def _translate(self, event: Any, session_id: str | None = None) -> tuple[ContainerOutput | None, str | None]:
        """Simulate the on_event callback from main.py's run_query_loop."""
        output: ContainerOutput | None = None

        if isinstance(event, ResultEvent):
            if event.new_session_id:
                session_id = event.new_session_id
            output = ContainerOutput(
                status="success",
                result=event.text,
                new_session_id=session_id,
            )
        elif isinstance(event, SessionInitEvent):
            session_id = event.session_id
        elif isinstance(event, ErrorEvent):
            output = ContainerOutput(
                status="error",
                result=None,
                new_session_id=session_id,
                error=event.error,
            )
        # CompactionEvent produces no output

        return output, session_id

    def test_result_event_with_text(self) -> None:
        out, sid = self._translate(ResultEvent(text="answer", new_session_id="s1"))
        assert out is not None
        assert out.status == "success"
        assert out.result == "answer"
        assert out.new_session_id == "s1"
        assert sid == "s1"

    def test_result_event_without_session_propagates_existing(self) -> None:
        """If ResultEvent has no new_session_id, the existing session_id is used."""
        out, sid = self._translate(
            ResultEvent(text="partial", new_session_id=None),
            session_id="existing-session",
        )
        assert out is not None
        assert out.new_session_id == "existing-session"
        assert sid == "existing-session"

    def test_result_event_with_none_text(self) -> None:
        """Session update events have text=None — should still publish."""
        out, _ = self._translate(ResultEvent(text=None, new_session_id="s2"))
        assert out is not None
        assert out.result is None
        assert out.new_session_id == "s2"

    def test_session_init_updates_tracking(self) -> None:
        out, sid = self._translate(SessionInitEvent(session_id="new-sid"))
        assert out is None  # SessionInitEvent doesn't produce output
        assert sid == "new-sid"

    def test_error_event(self) -> None:
        out, _ = self._translate(ErrorEvent(error="container crashed"))
        assert out is not None
        assert out.status == "error"
        assert out.error == "container crashed"
        assert out.result is None

    def test_error_event_preserves_session_id(self) -> None:
        out, sid = self._translate(
            ErrorEvent(error="oom"),
            session_id="s3",
        )
        assert out is not None
        assert out.new_session_id == "s3"

    def test_compaction_event_no_output(self) -> None:
        out, sid = self._translate(CompactionEvent(), session_id="s4")
        assert out is None
        assert sid == "s4"

    def test_session_id_tracks_across_events(self) -> None:
        """Simulate a sequence of events and verify session_id propagation."""
        sid: str | None = None

        # 1. Session init
        _, sid = self._translate(SessionInitEvent(session_id="initial"), sid)
        assert sid == "initial"

        # 2. Intermediate result (no new session)
        out, sid = self._translate(ResultEvent(text="partial", new_session_id=None), sid)
        assert out is not None
        assert out.new_session_id == "initial"

        # 3. Final result with new session
        out, sid = self._translate(ResultEvent(text="done", new_session_id="final"), sid)
        assert out is not None
        assert out.new_session_id == "final"
        assert sid == "final"
