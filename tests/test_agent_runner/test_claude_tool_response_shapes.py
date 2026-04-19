"""Edge-case coverage for Claude's PostToolUse tool_response normalization.

Claude SDK's PostToolUse hook passes `tool_response` in one of several
shapes depending on the tool that produced it:

  - str       for simple tools (Bash returning stdout etc.)
  - list      for tools that emit multiple blocks
  - dict      for MCP tools: {"content": [{"text": "..."}], "isError": True/False}
  - None      defensive: missing key
  - malformed defensive: dict with unexpected keys

_tool_response_text() in claude_backend.py normalizes all of these into
(text, is_error). A bug there routes tool success-vs-failure wrong:

  - If is_error detection misses the dict form, failing MCP tools show
    up as PostToolUse (not PostToolUseFailure) and DLP/audit code
    treats a failed tool call as successful.
  - If str -> (str, False) is dropped, all Bash output is invisible
    to PostToolUse handlers.
  - If list content is concatenated without the "text" field extracted,
    handlers see serialized block dicts instead of readable text.

These tests drive the PostToolUse callback directly with each shape
and assert the correct (text, route, is_error) result.
"""

from __future__ import annotations

import sys
import types
from typing import Any

_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type(
    "ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None}
)  # type: ignore[attr-defined]
_fake_sdk.HookMatcher = type(
    "HookMatcher",
    (),
    {"__init__": lambda self, **kw: setattr(self, "hooks", kw.get("hooks"))},
)  # type: ignore[attr-defined]
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)


from agent_runner import claude_backend  # noqa: E402


class _RecordingHookMatcher:
    def __init__(self, hooks: list[Any] | None = None, **_kw: Any) -> None:
        self.hooks = list(hooks) if hooks else []


claude_backend.HookMatcher = _RecordingHookMatcher  # type: ignore[assignment]


from agent_runner.hooks import (  # noqa: E402
    HookRegistry,
    ToolResultEvent,
    ToolResultVerdict,
)


class _Recorder:
    def __init__(self, append: str | None = None) -> None:
        self.success: list[ToolResultEvent] = []
        self.failure: list[ToolResultEvent] = []
        self._append = append

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        self.success.append(event)
        if self._append is not None:
            return ToolResultVerdict(appended_context=self._append)
        return None

    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        self.failure.append(event)


def _cb(append: str | None = None) -> tuple[Any, _Recorder]:
    rec = _Recorder(append=append)
    reg = HookRegistry()
    reg.register(rec)
    matchers = claude_backend._build_hook_callbacks(reg)
    return matchers["PostToolUse"][0].hooks[0], rec


# ---------------------------------------------------------------------------
# Success shapes
# ---------------------------------------------------------------------------


async def test_string_response_routes_success_and_preserves_text() -> None:
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "Bash",
            "tool_input": {"cmd": "ls"},
            "tool_response": "file1\nfile2\n",
        },
        "id-1",
        None,
    )
    assert len(rec.success) == 1
    assert rec.failure == []
    assert rec.success[0].tool_result == "file1\nfile2\n"
    assert rec.success[0].is_error is False


async def test_list_response_concatenates_text_blocks() -> None:
    """List of {type:text, text:...} dicts — join text fields in order."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": [
                {"type": "text", "text": "line1\n"},
                {"type": "text", "text": "line2\n"},
            ],
        },
        "id-1",
        None,
    )
    assert rec.success[0].tool_result == "line1\nline2\n"
    assert rec.success[0].is_error is False


async def test_list_response_mixes_strings_and_dicts() -> None:
    """Tolerate a list containing both raw strings and text dicts —
    some MCP adapters produce either form depending on the tool."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "mcp__ext__search",
            "tool_input": {},
            "tool_response": ["plain ", {"type": "text", "text": "dict"}],
        },
        "id-1",
        None,
    )
    assert rec.success[0].tool_result == "plain dict"


async def test_dict_content_list_extracted() -> None:
    """MCP-style success dict: {"content": [...], "isError": False}."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "mcp__rolemesh__send_message",
            "tool_input": {},
            "tool_response": {
                "content": [{"type": "text", "text": "sent"}],
                "isError": False,
            },
        },
        "id-1",
        None,
    )
    assert rec.success[0].tool_result == "sent"
    assert rec.success[0].is_error is False
    assert rec.failure == []


async def test_dict_content_string_not_list() -> None:
    """Some tools return {"content": "plain text"} without a block list."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "mcp__ext__tool",
            "tool_input": {},
            "tool_response": {"content": "inline string content"},
        },
        "id-1",
        None,
    )
    assert rec.success[0].tool_result == "inline string content"


# ---------------------------------------------------------------------------
# Failure routing
# ---------------------------------------------------------------------------


async def test_dict_is_error_routes_to_failure_handler() -> None:
    """{"content": [...], "isError": True} — MUST route to failure path,
    not success path. Mirror audit handlers rely on this split."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "mcp__linear__create_issue",
            "tool_input": {"title": "x"},
            "tool_response": {
                "content": [{"type": "text", "text": "API quota exceeded"}],
                "isError": True,
            },
        },
        "id-1",
        None,
    )
    assert rec.success == []
    assert len(rec.failure) == 1
    assert rec.failure[0].is_error is True
    assert rec.failure[0].tool_result == "API quota exceeded"


async def test_dict_snake_case_is_error_also_routes_to_failure() -> None:
    """Defensive: some producers emit snake_case "is_error" instead of
    the MCP canonical "isError". Accept both so handlers don't depend
    on upstream naming conventions."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "mcp__ext__tool",
            "tool_input": {},
            "tool_response": {
                "content": [{"type": "text", "text": "nope"}],
                "is_error": True,
            },
        },
        "id-1",
        None,
    )
    assert rec.success == []
    assert len(rec.failure) == 1


# ---------------------------------------------------------------------------
# Defensive: missing/None tool_response
# ---------------------------------------------------------------------------


async def test_missing_tool_response_routes_to_success_with_empty_text() -> None:
    """If tool_response is absent/None (defensive), treat as success
    with empty text. Don't raise — Claude SDK has emitted None in the
    past for void tools."""
    cb, rec = _cb()
    result = await cb(
        {"tool_name": "Bash", "tool_input": {"cmd": "noop"}, "tool_response": None},
        "id-1",
        None,
    )
    assert result == {}
    assert len(rec.success) == 1
    assert rec.success[0].tool_result == ""
    assert rec.success[0].is_error is False


async def test_missing_tool_response_key_entirely() -> None:
    """Field not present at all — getattr-like dispatch should yield
    None and still route to success with empty text."""
    cb, rec = _cb()
    await cb(
        {"tool_name": "Bash", "tool_input": {}},
        "id-1",
        None,
    )
    assert len(rec.success) == 1
    assert rec.failure == []
    assert rec.success[0].tool_result == ""


async def test_dict_without_content_key() -> None:
    """Defensive: a dict response without any content key. No crash,
    routes to success with empty text and is_error=False (since the
    dict had no error marker)."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "mcp__foo__bar",
            "tool_input": {},
            "tool_response": {"some_other_field": "ignored"},
        },
        "id-1",
        None,
    )
    assert len(rec.success) == 1
    assert rec.success[0].tool_result == ""
    assert rec.success[0].is_error is False


async def test_unsupported_shape_does_not_crash() -> None:
    """An int/float response (implausible but defensive): normalize to
    empty text. The contract is: never raise from the bridge."""
    cb, rec = _cb()
    await cb(
        {
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": 42,
        },
        "id-1",
        None,
    )
    assert len(rec.success) == 1
    assert rec.success[0].tool_result == ""


# ---------------------------------------------------------------------------
# Shape x append COMBINATIONS
#
# Gap closed: earlier tests verified normalization (str/list/dict -> text)
# and earlier parity tests verified append (single str response -> append).
# Neither exercised the combination — if a bridge mutation made "append
# only flow through on the str path", MCP-shaped success dicts would
# silently lose their audit/DLP context and no test would catch it.
# ---------------------------------------------------------------------------


async def test_mcp_dict_success_with_handler_append_returns_additional_context() -> None:
    """MCP tool success dict + handler appends -> additionalContext appears
    in the Claude-shaped return AND carries the handler's string."""
    cb, rec = _cb(append="AUDIT: policy check ok")
    result = await cb(
        {
            "tool_name": "mcp__linear__create_issue",
            "tool_input": {"title": "x"},
            "tool_response": {
                "content": [{"type": "text", "text": "issue created"}],
                "isError": False,
            },
        },
        "id-1",
        None,
    )

    assert result == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "AUDIT: policy check ok",
        }
    }
    # Handler observed the normalized text, not the raw dict
    assert rec.success[0].tool_result == "issue created"
    assert rec.failure == []


async def test_list_response_with_handler_append_returns_additional_context() -> None:
    """Multi-block list tool_response + handler appends -> additionalContext
    appears. Mutation: if append branch only runs on isinstance(tool_response, str)
    the list path drops the append silently."""
    cb, rec = _cb(append="AUDIT-list")
    result = await cb(
        {
            "tool_name": "Bash",
            "tool_input": {},
            "tool_response": [
                {"type": "text", "text": "line1\n"},
                {"type": "text", "text": "line2\n"},
            ],
        },
        "id-1",
        None,
    )

    assert (
        result["hookSpecificOutput"]["additionalContext"] == "AUDIT-list"
    )
    # Flattened text preserved through to the handler
    assert rec.success[0].tool_result == "line1\nline2\n"


async def test_mcp_dict_failure_with_handler_append_does_not_return_additional_context() -> None:
    """Failure path MUST route to on_post_tool_use_failure, NOT to
    on_post_tool_use. Even if an append handler is registered, a
    failed tool must not produce an additionalContext — the error
    branch doesn't have an append slot, and adding one would let a
    success-path handler silently mask errors."""
    cb, rec = _cb(append="AUDIT-success-only")
    result = await cb(
        {
            "tool_name": "mcp__linear__create_issue",
            "tool_input": {},
            "tool_response": {
                "content": [{"type": "text", "text": "quota exceeded"}],
                "isError": True,
            },
        },
        "id-1",
        None,
    )

    # No additionalContext — failure branch returns plain {}
    assert result == {}
    # Routed to failure handler only
    assert rec.success == []
    assert len(rec.failure) == 1
    assert rec.failure[0].is_error is True
    assert rec.failure[0].tool_result == "quota exceeded"
