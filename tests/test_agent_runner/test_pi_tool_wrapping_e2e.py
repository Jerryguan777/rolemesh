"""End-to-end test of the Pi lazy tool-wrapping chain.

What this test exercises that parity tests do not:

  tool.execute(...)
    -> _LazyWrappedTool  (from _wrap_tools_lazy in sdk.py)
       -> reads runner_ref["current"]
       -> delegates to _ExtensionWrappedTool
          -> runner.emit_tool_call   -> bridge ext's handle_tool_call
                                      -> HookRegistry.emit_pre_tool_use
          -> inner.execute(...)
          -> runner.emit_tool_result -> bridge ext's handle_tool_result
                                      -> HookRegistry.emit_post_tool_use (or _failure)

If ANY link breaks (e.g. _wrap_tools_lazy forgets to proxy, lazy proxy
forgets to read the ref at execute-time, or the bridge extension handlers
aren't registered against the right event name), hooks will silently not
fire on real tool calls — and only a full-chain test would catch it.

These tests cover both types of tool names the regex would see in
production:

  - custom tools (e.g. "send_message") representing in-process
    MCP-backed tools from create_rolemesh_tools / external MCP tools
    from load_mcp_tools — both arrive as AgentTool instances and go
    through the same `custom_tools` channel.
  - built-in Pi tool names ("bash", "read", etc.) — same wrapping.

Bugs this test is designed to catch:

  - Install runner AFTER _wrap_tools_lazy but references stale ref
  - _LazyWrappedTool falls through to inner.execute when runner IS
    bound (passes PreToolUse silently)
  - Block verdict raised by bridge ext doesn't actually prevent the
    inner tool.execute() call
  - PostToolUse appended_context doesn't reach the final content list
  - is_error from the inner tool isn't routed to the failure handler
"""

from __future__ import annotations

import asyncio  # noqa: TC003 — used at runtime in async signatures
from typing import Any

import pytest

from agent_runner.hooks import (
    HookRegistry,
    ToolCallEvent,
    ToolCallVerdict,
    ToolResultEvent,
    ToolResultVerdict,
)
from agent_runner.pi_backend import _build_bridge_extension
from pi.agent.types import AgentTool, AgentToolResult
from pi.ai.types import TextContent
from pi.coding_agent.core.extensions.loader import create_extension_runtime
from pi.coding_agent.core.extensions.runner import ExtensionRunner
from pi.coding_agent.core.sdk import _wrap_tools_lazy

# ---------------------------------------------------------------------------
# Minimal AgentTool fakes — one success, one failure
# ---------------------------------------------------------------------------


class _EchoTool(AgentTool):
    """Returns its params as text content; also counts executions."""

    def __init__(self, name: str = "send_message") -> None:
        self._name = name
        self.executed_with: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def label(self) -> str:
        return "Echo"

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        self.executed_with.append(dict(params))
        return AgentToolResult(
            content=[TextContent(text=f"echoed {params}")],
            details=None,
        )


class _FailingTool(_EchoTool):
    """Raises from inside execute — mimics an MCP tool crashing."""

    async def execute(  # type: ignore[override]
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: Any = None,
    ) -> AgentToolResult:
        self.executed_with.append(dict(params))
        raise RuntimeError("inner tool exploded")


class _Recorder:
    def __init__(self) -> None:
        self.pre_tool_use: list[ToolCallEvent] = []
        self.post_tool_use: list[ToolResultEvent] = []
        self.post_tool_use_failure: list[ToolResultEvent] = []
        self.append_context: str | None = None
        self.block: tuple[bool, str | None] = (False, None)

    async def on_pre_tool_use(self, event: ToolCallEvent) -> ToolCallVerdict | None:
        self.pre_tool_use.append(event)
        blocked, reason = self.block
        if blocked:
            return ToolCallVerdict(block=True, reason=reason or "no")
        return None

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        self.post_tool_use.append(event)
        if self.append_context is not None:
            return ToolResultVerdict(appended_context=self.append_context)
        return None

    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        self.post_tool_use_failure.append(event)


def _setup() -> tuple[dict[str, Any], HookRegistry, _Recorder]:
    """Build a lazy wrapper + bind an ExtensionRunner via the ref.

    Returns the runner_ref so the test can verify "ref-not-bound" pass-through
    separately from "ref-bound" interception.
    """
    recorder = _Recorder()
    registry = HookRegistry()
    registry.register(recorder)

    ref: dict[str, Any] = {}
    bridge = _build_bridge_extension(registry)
    runtime = create_extension_runtime()
    runner = ExtensionRunner(extensions=[bridge], runtime=runtime, cwd="/tmp")
    ref["current"] = runner
    return ref, registry, recorder


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_wrapped_tool_fires_pre_and_post_hooks_with_recorded_params() -> None:
    """Sanity-chain: pre and post hooks fire exactly once per execute,
    with the original params visible in both events."""
    ref, _, recorder = _setup()
    tool = _EchoTool(name="send_message")
    wrapped = _wrap_tools_lazy([tool], ref)[0]

    result = await wrapped.execute(
        tool_call_id="id-1",
        params={"to": "jerry", "body": "hi"},
    )

    # Inner tool ran (not blocked, not replaced)
    assert tool.executed_with == [{"to": "jerry", "body": "hi"}]
    # Result from inner tool makes it back
    assert any(
        isinstance(b, TextContent) and "echoed" in b.text for b in result.content
    )
    # Hooks observed the call
    assert len(recorder.pre_tool_use) == 1
    assert recorder.pre_tool_use[0].tool_name == "send_message"
    assert recorder.pre_tool_use[0].tool_input == {"to": "jerry", "body": "hi"}
    assert recorder.pre_tool_use[0].tool_call_id == "id-1"
    assert len(recorder.post_tool_use) == 1
    assert "echoed" in recorder.post_tool_use[0].tool_result
    assert recorder.post_tool_use_failure == []


# ---------------------------------------------------------------------------
# Pre-tool-use blocking
# ---------------------------------------------------------------------------


async def test_pre_tool_use_block_prevents_inner_execute() -> None:
    """Block verdict in a PreToolUse handler must prevent the inner
    tool.execute() from running. The inner tool should see zero calls.
    The wrapper in Pi raises RuntimeError when blocked, which Agent
    catches; for the test we just assert the raise."""
    ref, _, recorder = _setup()
    recorder.block = (True, "not allowed")
    tool = _EchoTool(name="send_message")
    wrapped = _wrap_tools_lazy([tool], ref)[0]

    with pytest.raises(RuntimeError, match="not allowed"):
        await wrapped.execute(tool_call_id="id-1", params={"x": 1})

    assert tool.executed_with == [], "inner tool must NOT run after block"
    assert len(recorder.pre_tool_use) == 1
    # Post not fired because the call was blocked pre-execute
    assert recorder.post_tool_use == []


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


async def test_inner_tool_failure_routes_to_failure_handler() -> None:
    """Inner tool raises -> PostToolUseFailure called, PostToolUse NOT
    called. The is_error signal is preserved end-to-end."""
    ref, _, recorder = _setup()
    tool = _FailingTool(name="search")
    wrapped = _wrap_tools_lazy([tool], ref)[0]

    with pytest.raises(RuntimeError, match="inner tool exploded"):
        await wrapped.execute(tool_call_id="id-1", params={"q": "x"})

    assert recorder.post_tool_use == []
    assert len(recorder.post_tool_use_failure) == 1
    assert recorder.post_tool_use_failure[0].is_error is True
    assert "inner tool exploded" in recorder.post_tool_use_failure[0].tool_result


# ---------------------------------------------------------------------------
# PostToolUse appended context reaches the agent's view
# ---------------------------------------------------------------------------


async def test_post_tool_use_append_context_lands_in_result_content() -> None:
    """A handler appends 'AUDIT' -> the final AgentToolResult.content
    seen by Agent must contain BOTH the original echoed text AND the
    AUDIT text block. Mutation: dropping the append in Pi's bridge or
    in _ExtensionWrappedTool's content merging would hide the append
    from the LLM context entirely."""
    ref, _, recorder = _setup()
    recorder.append_context = "AUDIT: tool was called"
    tool = _EchoTool(name="send_message")
    wrapped = _wrap_tools_lazy([tool], ref)[0]

    result = await wrapped.execute(
        tool_call_id="id-1", params={"msg": "hello"}
    )

    text_blocks = [b.text for b in result.content if isinstance(b, TextContent)]
    assert any("echoed" in t for t in text_blocks), "original content missing"
    assert any(
        "AUDIT: tool was called" in t for t in text_blocks
    ), "handler-appended context missing from final result"


# ---------------------------------------------------------------------------
# Unbound ref: lazy proxy must pass through cleanly (no crash)
# ---------------------------------------------------------------------------


async def test_unbound_ref_passes_through_without_hook_emission() -> None:
    """_wrap_tools_lazy is installed by create_agent_session BEFORE the
    caller binds the runner. If a tool somehow executes between those two
    points (shouldn't normally happen but defense in depth), the lazy
    proxy must fall through to inner.execute with no crash and no hook
    emission. Losing this property means a race during agent startup
    could raise NoneType errors."""
    empty_ref: dict[str, Any] = {}  # no "current" key set
    tool = _EchoTool()
    wrapped = _wrap_tools_lazy([tool], empty_ref)[0]

    result = await wrapped.execute(tool_call_id="id-1", params={"x": 1})

    assert tool.executed_with == [{"x": 1}]
    assert any(
        isinstance(b, TextContent) and "echoed" in b.text for b in result.content
    )


# ---------------------------------------------------------------------------
# Tool name preserved for in-process MCP ("mcp__rolemesh__*") and for
# external MCP ("mcp__other__*") — the bridge reads the name, it MUST
# NOT rewrite or strip prefixes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["mcp__rolemesh__send_message", "mcp__external_server__search", "bash"],
)
async def test_tool_name_preserved_across_bridge(tool_name: str) -> None:
    """All three namespaces observed in the real codebase:
      - in-process rolemesh MCP   -> mcp__rolemesh__*
      - external MCP server       -> mcp__<server-name>__*
      - built-in Pi tool          -> lowercased name
    Each must produce a hook event whose tool_name is the ORIGINAL
    identifier — no stripping/canonicalization by the bridge."""
    ref, _, recorder = _setup()
    tool = _EchoTool(name=tool_name)
    wrapped = _wrap_tools_lazy([tool], ref)[0]

    await wrapped.execute(tool_call_id="id-1", params={})

    assert recorder.pre_tool_use[0].tool_name == tool_name
    assert recorder.post_tool_use[0].tool_name == tool_name
