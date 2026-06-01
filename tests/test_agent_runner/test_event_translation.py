"""Tests for BackendEvent → ContainerOutput translation in the NATS bridge.

The bridge's translation logic lives in ``agent_runner.main.event_to_output``
(a pure function called by ``run_query_loop.on_event``). These tests drive
that real function directly — no NATS, and crucially no inline copy of the
mapping. An earlier version of this file re-implemented the dispatch in a
``_translate`` helper and asserted against the copy; that copy had silently
drifted from production (it preserved session_id on ErrorEvent while the real
code nulls it), so the test passed while asserting the opposite of reality.
Testing the real function is the whole point.
"""

from __future__ import annotations

import json

from agent_runner.backend import (
    CompactionEvent,
    ErrorEvent,
    ResultEvent,
    RunningEvent,
    SafetyBlockEvent,
    SessionInitEvent,
    StoppedEvent,
    ToolUseEvent,
    UsageSnapshot,
    tool_input_preview,
)
from agent_runner.main import ContainerOutput, event_to_output


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

    def test_is_final_default_true_omitted_from_dict(self) -> None:
        """is_final=True is the legacy semantics (one reply per turn) — to keep
        the wire payload minimal and backwards-compatible with older hosts
        that don't know about isFinal, we only emit the key when False."""
        out = ContainerOutput(status="success", result="x", is_final=True)
        d = out.to_dict()
        assert "isFinal" not in d

    def test_is_final_false_emitted_as_camel_case(self) -> None:
        """is_final=False MUST land in the JSON payload (camelCase). The host
        uses this to skip notify_idle until the batch settles."""
        out = ContainerOutput(status="success", result="reply1", is_final=False)
        d = out.to_dict()
        assert d["isFinal"] is False
        # And it must survive a JSON roundtrip.
        assert json.loads(json.dumps(d))["isFinal"] is False


class TestEventToOutput:
    """Drive the real ``event_to_output`` across every event type, focusing
    on the contract edges: which events publish, how session_id propagates,
    and the deliberate session_id=None on the error/block paths."""

    def test_result_event_adopts_its_new_session_id(self) -> None:
        out, sid = event_to_output(ResultEvent(text="answer", new_session_id="s1"), None)
        assert out is not None
        assert out.status == "success"
        assert out.result == "answer"
        assert out.new_session_id == "s1"
        assert sid == "s1"

    def test_result_event_without_new_session_keeps_existing(self) -> None:
        out, sid = event_to_output(
            ResultEvent(text="partial", new_session_id=None), "existing"
        )
        assert out is not None
        assert out.new_session_id == "existing"
        assert sid == "existing"

    def test_result_event_with_none_text_still_publishes(self) -> None:
        """Session-update results carry text=None but must still publish."""
        out, _ = event_to_output(ResultEvent(text=None, new_session_id="s2"), None)
        assert out is not None
        assert out.result is None
        assert out.new_session_id == "s2"

    def test_session_init_updates_tracking_without_publishing(self) -> None:
        out, sid = event_to_output(SessionInitEvent(session_id="new-sid"), "old")
        assert out is None
        assert sid == "new-sid"

    def test_compaction_event_is_a_no_op(self) -> None:
        out, sid = event_to_output(CompactionEvent(), "s4")
        assert out is None
        assert sid == "s4"

    def test_running_event_publishes_progress_only(self) -> None:
        out, sid = event_to_output(RunningEvent(), "s5")
        assert out is not None
        assert out.status == "running"
        assert out.result is None
        assert sid == "s5"  # progress must not disturb session tracking
        assert "newSessionId" not in out.to_dict()

    def test_tool_use_event_carries_metadata(self) -> None:
        out, _ = event_to_output(ToolUseEvent(tool="Bash", input_preview="ls /tmp"), None)
        assert out is not None
        assert out.status == "tool_use"
        assert out.metadata == {"tool": "Bash", "input": "ls /tmp"}
        d = out.to_dict()
        assert "newSessionId" not in d and "error" not in d

    def test_stopped_event_publishes_with_current_session(self) -> None:
        out, sid = event_to_output(StoppedEvent(), "s6")
        assert out is not None
        assert out.status == "stopped"
        assert out.new_session_id == "s6"
        assert sid == "s6"

    def test_error_event_drops_session_id(self) -> None:
        """Regression: the previous mirror test asserted the session_id was
        PRESERVED on error. Production deliberately nulls new_session_id (a
        stale resume id is the usual error source; forwarding it death-loops
        the scheduler). Pin the real behavior."""
        out, sid = event_to_output(ErrorEvent(error="container crashed"), "stale-sid")
        assert out is not None
        assert out.status == "error"
        assert out.error == "container crashed"
        assert out.result is None
        assert out.new_session_id is None
        # The tracked session_id itself is untouched; only the published
        # payload drops it.
        assert sid == "stale-sid"

    def test_safety_block_drops_session_id(self) -> None:
        out, sid = event_to_output(
            SafetyBlockEvent(stage="input_prompt", reason="blocked"), "stale-sid"
        )
        assert out is not None
        assert out.status == "safety_blocked"
        assert out.result == "blocked"
        assert out.new_session_id is None
        assert sid == "stale-sid"

    def test_is_final_false_propagates(self) -> None:
        out, _ = event_to_output(ResultEvent(text="reply1", is_final=False), None)
        assert out is not None
        assert out.is_final is False
        assert out.to_dict()["isFinal"] is False

    def test_is_final_true_omitted_from_wire(self) -> None:
        out, _ = event_to_output(ResultEvent(text="final", is_final=True), None)
        assert out is not None
        assert "isFinal" not in out.to_dict()

    def test_session_id_threads_through_an_event_sequence(self) -> None:
        sid: str | None = None
        _, sid = event_to_output(SessionInitEvent(session_id="initial"), sid)
        assert sid == "initial"
        out, sid = event_to_output(ResultEvent(text="partial", new_session_id=None), sid)
        assert out is not None and out.new_session_id == "initial"
        out, sid = event_to_output(ResultEvent(text="done", new_session_id="final"), sid)
        assert out is not None and out.new_session_id == "final"
        assert sid == "final"


class TestUsagePropagationThroughBridge:
    """Pin the bridge's metadata.usage placement for all four terminal
    events via the real ``event_to_output``. The orchestrator's
    _extract_usage(metadata) keys off the literal ``usage`` subkey — any
    drift here silently breaks DB persistence."""

    def test_result_event_with_usage_lands_in_metadata(self) -> None:
        snap = UsageSnapshot(
            input_tokens=100, output_tokens=50, cost_usd=0.001,
            model_id="claude-sonnet-4-6", cost_source="sdk",
        )
        out, _ = event_to_output(ResultEvent(text="hi", usage=snap), None)
        assert out is not None and out.metadata is not None
        assert out.metadata["usage"]["input_tokens"] == 100
        assert out.metadata["usage"]["cost_source"] == "sdk"

    def test_error_event_with_usage_carries_burnt_tokens(self) -> None:
        snap = UsageSnapshot(input_tokens=200, output_tokens=10)
        out, _ = event_to_output(ErrorEvent(error="boom", usage=snap), None)
        assert out is not None and out.metadata is not None
        assert out.metadata["usage"]["input_tokens"] == 200

    def test_stopped_event_with_usage_carries_aborted_tokens(self) -> None:
        snap = UsageSnapshot(input_tokens=300, output_tokens=20)
        out, _ = event_to_output(StoppedEvent(usage=snap), None)
        assert out is not None and out.metadata is not None
        assert out.metadata["usage"]["input_tokens"] == 300

    def test_safety_block_with_usage_keeps_stage_and_usage(self) -> None:
        """Output-stage block: stage AND usage must coexist in metadata,
        neither overwriting the other."""
        snap = UsageSnapshot(input_tokens=50, output_tokens=5)
        out, _ = event_to_output(
            SafetyBlockEvent(stage="model_output", reason="rule fired", rule_id="r-1", usage=snap),
            None,
        )
        assert out is not None and out.metadata is not None
        assert out.metadata["stage"] == "model_output"
        assert out.metadata["rule_id"] == "r-1"
        assert out.metadata["usage"]["input_tokens"] == 50

    def test_result_event_without_usage_omits_metadata(self) -> None:
        """The byte-equality property: usage=None produces no metadata key."""
        out, _ = event_to_output(ResultEvent(text="hi", usage=None), None)
        assert out is not None
        assert out.metadata is None
        assert "metadata" not in out.to_dict()

    def test_safety_block_without_usage_keeps_stage_only_metadata(self) -> None:
        """Input-stage block (no LLM call → no usage). The wire metadata is
        the legacy stage-only dict, exactly as before."""
        out, _ = event_to_output(SafetyBlockEvent(stage="input_prompt", reason="blocked"), None)
        assert out is not None
        assert out.metadata == {"stage": "input_prompt"}


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
        assert tool_input_preview("Grep", {"pattern": "TODO"}) == "TODO"

    def test_glob_path_fallback(self) -> None:
        assert tool_input_preview("Glob", {"pattern": "**/*.py"}) == "**/*.py"

    def test_websearch_query(self) -> None:
        assert tool_input_preview("WebSearch", {"query": "rust async"}) == "rust async"

    def test_webfetch_url_fallback(self) -> None:
        assert tool_input_preview("WebFetch", {"url": "https://example.com"}) == "https://example.com"

    def test_mcp_namespaced_tool_strips_prefix(self) -> None:
        """MCP tools arrive as mcp__<server>__<tool> — must strip before matching."""
        assert tool_input_preview("mcp__rolemesh__bash", {"command": "pwd"}) == "pwd"
        assert tool_input_preview("mcp__external__read", {"file_path": "/app/x.py"}) == "/app/x.py"

    def test_unknown_tool_returns_empty(self) -> None:
        assert tool_input_preview("SomeCustomTool", {"arg": "value"}) == ""

    def test_truncates_long_preview(self) -> None:
        long_cmd = "x" * 200
        out = tool_input_preview("Bash", {"command": long_cmd})
        assert len(out) == 80
        assert out == "x" * 80

    def test_missing_field_returns_empty(self) -> None:
        assert tool_input_preview("Bash", {}) == ""
        assert tool_input_preview("Read", {}) == ""

    def test_non_string_values_stringified_safely(self) -> None:
        out = tool_input_preview("Task", {"description": 42})
        assert out == "42"
