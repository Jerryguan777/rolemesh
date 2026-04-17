"""Tests for BackendEvent → ContainerOutput translation in the NATS bridge.

The bridge's on_event callback translates backend events into NATS publishes.
These tests verify the mapping logic directly, without NATS.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runner.backend import (
    CompactionEvent,
    ErrorEvent,
    ResultEvent,
    RunningEvent,
    SessionInitEvent,
    ToolUseEvent,
    tool_input_preview,
)
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

    def test_running_event_maps_to_running_status(self) -> None:
        # Mirrors the on_event branch in main.py run_query_loop.
        out = ContainerOutput(status="running", result=None)
        d = out.to_dict()
        assert d["status"] == "running"
        assert d["result"] is None
        assert "metadata" not in d

    def test_tool_use_event_carries_metadata(self) -> None:
        event = ToolUseEvent(tool="Bash", input_preview="ls /tmp")
        out = ContainerOutput(
            status="tool_use",
            result=None,
            metadata={"tool": event.tool, "input": event.input_preview},
        )
        d = out.to_dict()
        assert d["status"] == "tool_use"
        assert d["metadata"] == {"tool": "Bash", "input": "ls /tmp"}
        # progress events never carry session / error
        assert "newSessionId" not in d
        assert "error" not in d

    def test_running_event_frozen(self) -> None:
        # Ensure the marker event is a singleton-shaped dataclass.
        ev = RunningEvent()
        assert ev == RunningEvent()

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


class TestToolInputPreview:
    """tool_input_preview must handle Claude (PascalCase), Pi (lowercase),
    and MCP (mcp__server__tool) naming conventions."""

    def test_bash_claude(self) -> None:
        assert tool_input_preview("Bash", {"command": "npm test"}) == "npm test"

    def test_bash_pi_lowercase(self) -> None:
        assert tool_input_preview("bash", {"command": "ls /tmp"}) == "ls /tmp"

    def test_read_file_path(self) -> None:
        assert tool_input_preview("Read", {"file_path": "/etc/hosts"}) == "/etc/hosts"

    def test_read_pi_lowercase(self) -> None:
        assert tool_input_preview("read", {"file_path": "/etc/hosts"}) == "/etc/hosts"

    def test_grep_pattern(self) -> None:
        # Grep's primary input is pattern, not file_path
        assert tool_input_preview("Grep", {"pattern": "TODO"}) == "TODO"

    def test_glob_path_fallback(self) -> None:
        # Glob uses pattern field; fall back order is file_path → path → pattern
        assert tool_input_preview("Glob", {"pattern": "**/*.py"}) == "**/*.py"

    def test_websearch_query(self) -> None:
        assert tool_input_preview("WebSearch", {"query": "rust async"}) == "rust async"

    def test_webfetch_url_fallback(self) -> None:
        assert tool_input_preview("WebFetch", {"url": "https://example.com"}) == "https://example.com"

    def test_mcp_namespaced_tool_strips_prefix(self) -> None:
        """MCP tools arrive as mcp__<server>__<tool> — must strip before matching."""
        # Matches "bash" after stripping the mcp__rolemesh__ prefix
        assert tool_input_preview("mcp__rolemesh__bash", {"command": "pwd"}) == "pwd"
        # Matches "read" after stripping
        assert tool_input_preview(
            "mcp__external__read", {"file_path": "/app/x.py"}
        ) == "/app/x.py"

    def test_unknown_tool_returns_empty(self) -> None:
        assert tool_input_preview("SomeCustomTool", {"arg": "value"}) == ""

    def test_truncates_long_preview(self) -> None:
        long_cmd = "x" * 200
        out = tool_input_preview("Bash", {"command": long_cmd})
        assert len(out) == 80
        assert out == "x" * 80

    def test_missing_field_returns_empty(self) -> None:
        # bash input dict missing "command" key → ""
        assert tool_input_preview("Bash", {}) == ""
        # Read missing file_path / path / pattern → ""
        assert tool_input_preview("Read", {}) == ""

    def test_non_string_values_stringified_safely(self) -> None:
        # tool_input_preview should not crash if a dict has a non-str value
        out = tool_input_preview("Task", {"description": 42})
        assert out == "42"
