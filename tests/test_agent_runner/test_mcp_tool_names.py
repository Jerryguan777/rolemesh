"""Verify in-process (mcp__rolemesh__*) and external (mcp__<server>__*)
MCP tool names are preserved end-to-end by both backends' hook bridges.

Why this is a gap worth closing:

  Allow-lists in audit/DLP handlers typically key on tool_name. If a
  bridge ever canonicalizes, lowercases, or strips prefixes, handlers
  that say `if event.tool_name == "mcp__rolemesh__send_message"` would
  silently stop matching — and the failure is invisible (no exception,
  just audit records that never fire). Keeping this surface stable
  across refactors is a correctness-critical invariant.

Pi-side coverage of the wrapped-execution path lives in
test_pi_tool_wrapping_e2e.py::test_tool_name_preserved_across_bridge.
This file focuses on the Claude bridge's _build_hook_callbacks output,
which is what the Claude CLI subprocess actually invokes.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# Claude SDK stub + HookMatcher override, same pattern as test_hook_parity.py
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
    ToolCallEvent,
    ToolCallVerdict,
    ToolResultEvent,
)


class _Recorder:
    def __init__(self) -> None:
        self.pre_tool_use_events: list[ToolCallEvent] = []
        self.post_tool_use_events: list[ToolResultEvent] = []

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        self.pre_tool_use_events.append(event)
        return None

    async def on_post_tool_use(self, event: ToolResultEvent) -> None:
        self.post_tool_use_events.append(event)


def _claude_callbacks(registry: HookRegistry) -> dict[str, Any]:
    matchers = claude_backend._build_hook_callbacks(registry)
    return {event: matchers[event][0].hooks[0] for event in matchers}


# ---------------------------------------------------------------------------
# Tool names seen in production — all three variants must round-trip
# ---------------------------------------------------------------------------


# (tool_name, description used in tooltip)
_TOOL_NAMES = [
    ("mcp__rolemesh__send_message", "in-process RoleMesh MCP tool"),
    ("mcp__rolemesh__schedule_task", "in-process RoleMesh MCP tool"),
    ("mcp__linear__create_issue", "external MCP server tool"),
    ("mcp__filesystem__read_file", "external MCP server tool"),
    # Built-in tools: preserved by the bridge as-is, no mcp__ prefix.
    ("Bash", "built-in Claude tool"),
    ("WebFetch", "built-in Claude tool"),
]


@pytest.mark.parametrize("tool_name,_kind", _TOOL_NAMES)
async def test_pre_tool_use_preserves_tool_name_and_input(
    tool_name: str, _kind: str
) -> None:
    """The PreToolUse callback must pass the tool_name through verbatim.
    If the bridge ever canonicalizes the name — lowercasing,
    prefix-stripping, normalizing — audit/DLP handlers that match on
    the MCP-qualified name would silently stop firing."""
    recorder = _Recorder()
    registry = HookRegistry()
    registry.register(recorder)
    cb = _claude_callbacks(registry)["PreToolUse"]

    result = await cb(
        {"tool_name": tool_name, "tool_input": {"arg": 1}},
        "call-id-abc",
        None,
    )

    # Allow verdict empty (no handler blocked/modified)
    assert result == {}
    assert len(recorder.pre_tool_use_events) == 1
    observed = recorder.pre_tool_use_events[0]
    assert observed.tool_name == tool_name  # byte-for-byte
    assert observed.tool_input == {"arg": 1}
    assert observed.tool_call_id == "call-id-abc"


@pytest.mark.parametrize("tool_name,_kind", _TOOL_NAMES)
async def test_post_tool_use_preserves_tool_name(
    tool_name: str, _kind: str
) -> None:
    """Same invariant on the success-path PostToolUse callback."""
    recorder = _Recorder()
    registry = HookRegistry()
    registry.register(recorder)
    cb = _claude_callbacks(registry)["PostToolUse"]

    result = await cb(
        {
            "tool_name": tool_name,
            "tool_input": {"arg": 1},
            "tool_response": "ok",
        },
        "call-id-abc",
        None,
    )

    # No handler appended, so result must be an empty dict (not None, not a
    # hookSpecificOutput with empty additionalContext)
    assert result == {}
    assert len(recorder.post_tool_use_events) == 1
    assert recorder.post_tool_use_events[0].tool_name == tool_name


async def test_block_reason_surfaces_for_mcp_tool() -> None:
    """Concrete use case: a handler blocks a specific MCP tool by name.
    The deny response must carry the handler's reason unchanged so the
    agent (and audit log) records WHY the call was denied."""

    class _DenyLinear:
        async def on_pre_tool_use(
            self, event: ToolCallEvent
        ) -> ToolCallVerdict | None:
            if event.tool_name.startswith("mcp__linear__"):
                return ToolCallVerdict(
                    block=True, reason="linear API disabled by policy"
                )
            return None

    registry = HookRegistry()
    registry.register(_DenyLinear())
    cb = _claude_callbacks(registry)["PreToolUse"]

    blocked = await cb(
        {"tool_name": "mcp__linear__create_issue", "tool_input": {}},
        "id-1",
        None,
    )
    assert (
        blocked["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert (
        blocked["hookSpecificOutput"]["permissionDecisionReason"]
        == "linear API disabled by policy"
    )

    # Same handler against a different tool name — must NOT block
    allowed = await cb(
        {"tool_name": "mcp__rolemesh__send_message", "tool_input": {}},
        "id-2",
        None,
    )
    assert allowed == {}
