"""Cross-backend parity tests for the unified hook system.

The goal: the same HookHandler observed the same way across both backends.
We drive the hook *translation layers* directly:

  - Claude: claude_backend._build_hook_callbacks(registry) returns the
    SDK-shaped callback dict. We invoke them with the input_data shapes
    claude_agent_sdk produces.
  - Pi: pi_backend._build_bridge_extension(registry) returns an Extension
    whose handlers we invoke with the shapes Pi's ExtensionRunner produces.

This avoids mocking the entire LLM runtime while still validating that a
handler's observable effect is backend-neutral.

Each parity test parameterizes over ["claude", "pi"] and runs the SAME
handler code through each bridge. If one drifts from the other (e.g. the
Pi bridge forgets to route is_error → failure), the parity test fails
identically for the drifted backend.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Claude SDK stub (same pattern as test_claude_abort.py)
# ---------------------------------------------------------------------------

_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
_fake_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: setattr(self, "hooks", kw.get("hooks"))})  # type: ignore[attr-defined]
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)


from agent_runner import claude_backend, pi_backend  # noqa: E402


class _RecordingHookMatcher:
    """Stand-in for claude_agent_sdk.HookMatcher that preserves the hooks kwarg.

    The module-level sys.modules stub defined above is a coin toss across
    test modules because test_claude_abort.py registers its own stub first
    with setdefault(). Override claude_backend.HookMatcher locally so our
    bridge-callback extraction works regardless of which stub won the race.
    """

    def __init__(self, hooks: list[Any] | None = None, **_kw: Any) -> None:
        self.hooks = list(hooks) if hooks else []


claude_backend.HookMatcher = _RecordingHookMatcher  # type: ignore[assignment]

from agent_runner.hooks import (  # noqa: E402
    CompactionEvent,
    HookRegistry,
    ToolCallEvent,
    ToolCallVerdict,
    ToolResultEvent,
    ToolResultVerdict,
    UserPromptEvent,
    UserPromptVerdict,
)

# ---------------------------------------------------------------------------
# Shared observation handler — single source of truth used in both bridges
# ---------------------------------------------------------------------------


@dataclass
class CountingHandler:
    allow_tool: bool = True
    block_reason: str = "forbidden"
    append_context: str | None = None
    block_prompt: bool = False
    fail_in_pre_tool_use: bool = False
    pre_tool_use_calls: int = 0
    post_tool_use_calls: int = 0
    post_tool_use_failure_calls: int = 0
    pre_compact_calls: int = 0
    stop_calls: list[str] = field(default_factory=list)

    async def on_pre_tool_use(self, event: ToolCallEvent) -> ToolCallVerdict | None:
        self.pre_tool_use_calls += 1
        if self.fail_in_pre_tool_use:
            raise RuntimeError("boom")
        if not self.allow_tool:
            return ToolCallVerdict(block=True, reason=self.block_reason)
        return None

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        self.post_tool_use_calls += 1
        if self.append_context is not None:
            return ToolResultVerdict(appended_context=self.append_context)
        return None

    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        self.post_tool_use_failure_calls += 1

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        self.pre_compact_calls += 1

    async def on_user_prompt_submit(
        self, event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        if self.block_prompt:
            return UserPromptVerdict(block=True, reason="prompt forbidden")
        return None

    async def on_stop(self, event: Any) -> None:
        self.stop_calls.append(event.reason)


# ---------------------------------------------------------------------------
# Backend bridge accessors
# ---------------------------------------------------------------------------


def _claude_sdk_hooks(registry: HookRegistry) -> dict[str, Any]:
    matchers = claude_backend._build_hook_callbacks(registry)
    # Each value is [HookMatcher(hooks=[callable])]; pull out the callable
    flat = {event: matchers[event][0].hooks[0] for event in matchers}
    return flat


def _pi_bridge_handlers(registry: HookRegistry) -> dict[str, Any]:
    ext = pi_backend._build_bridge_extension(registry)
    return {event: ext.handlers[event][0] for event in ext.handlers}


# ---------------------------------------------------------------------------
# Parity: PreToolUse block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_pre_tool_use_block_parity(backend: str) -> None:
    """When a handler blocks, both backends surface a backend-shaped 'block'
    verdict AND the SAME HookRegistry.handler.call-count. Mutation target:
    if one bridge forgets to invoke the registry, only one side observes
    the call."""
    registry = HookRegistry()
    handler = CountingHandler(allow_tool=False, block_reason="no bash")
    registry.register(handler)

    if backend == "claude":
        cb = _claude_sdk_hooks(registry)["PreToolUse"]
        result = await cb(
            {"tool_name": "Bash", "tool_input": {"cmd": "rm -rf /"}},
            "tool-call-42",
            None,
        )
        assert (
            result["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == "no bash"
        )
    else:
        from pi.coding_agent.core.extensions.types import ToolCallEvent as PiEvt

        cb = _pi_bridge_handlers(registry)["tool_call"]
        result = await cb(
            PiEvt(
                tool_name="bash",
                tool_call_id="tool-call-42",
                input={"cmd": "rm -rf /"},
            ),
            None,
        )
        assert result == {"block": True, "reason": "no bash"}

    assert handler.pre_tool_use_calls == 1


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_pre_tool_use_allow_parity(backend: str) -> None:
    """No block + no modification ⇒ both backends return an empty
    response (empty dict / None). The agent proceeds."""
    registry = HookRegistry()
    registry.register(CountingHandler(allow_tool=True))

    if backend == "claude":
        cb = _claude_sdk_hooks(registry)["PreToolUse"]
        result = await cb(
            {"tool_name": "Bash", "tool_input": {"cmd": "ls"}}, "id-1", None
        )
        assert result == {}
    else:
        from pi.coding_agent.core.extensions.types import ToolCallEvent as PiEvt

        cb = _pi_bridge_handlers(registry)["tool_call"]
        result = await cb(
            PiEvt(tool_name="bash", tool_call_id="id-1", input={"cmd": "ls"}),
            None,
        )
        assert result is None


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_pre_tool_use_failclose_parity(backend: str) -> None:
    """A handler crash is translated to a block on BOTH backends — never
    allowed to fall through as an 'allow'."""
    registry = HookRegistry()
    registry.register(CountingHandler(fail_in_pre_tool_use=True))

    if backend == "claude":
        cb = _claude_sdk_hooks(registry)["PreToolUse"]
        result = await cb(
            {"tool_name": "Bash", "tool_input": {}}, "id-1", None
        )
        assert (
            result["hookSpecificOutput"]["permissionDecision"] == "deny"
        )
        assert "Hook system error" in result["hookSpecificOutput"]["permissionDecisionReason"]
    else:
        from pi.coding_agent.core.extensions.types import ToolCallEvent as PiEvt

        cb = _pi_bridge_handlers(registry)["tool_call"]
        result = await cb(
            PiEvt(tool_name="bash", tool_call_id="id-1", input={}), None
        )
        assert result is not None
        assert result["block"] is True
        assert "Hook system error" in result["reason"]


# ---------------------------------------------------------------------------
# Parity: PostToolUse append_context
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_post_tool_use_append_parity(backend: str) -> None:
    """Appended context shows up in both backends' own protocol shape.
    Verified by inspecting the return — the agent's eventual view of the
    tool result includes the handler-provided text."""
    registry = HookRegistry()
    handler = CountingHandler(append_context="AUDIT-LINE")
    registry.register(handler)

    if backend == "claude":
        cb = _claude_sdk_hooks(registry)["PostToolUse"]
        result = await cb(
            {
                "tool_name": "Bash",
                "tool_input": {"cmd": "ls"},
                "tool_response": "file1\nfile2\n",
            },
            "id-1",
            None,
        )
        assert (
            result["hookSpecificOutput"]["additionalContext"] == "AUDIT-LINE"
        )
    else:
        from pi.ai.types import TextContent
        from pi.coding_agent.core.extensions.types import (
            ToolResultEvent as PiEvt,
        )

        cb = _pi_bridge_handlers(registry)["tool_result"]
        result = await cb(
            PiEvt(
                tool_name="bash",
                tool_call_id="id-1",
                input={"cmd": "ls"},
                content=[TextContent(text="file1\nfile2\n")],
                is_error=False,
            ),
            None,
        )
        assert result is not None
        content = result["content"]
        # Original block kept; new TextContent appended with handler text.
        assert any(
            isinstance(b, TextContent) and b.text == "AUDIT-LINE" for b in content
        )
        assert any(
            isinstance(b, TextContent) and "file1\nfile2" in b.text for b in content
        )

    assert handler.post_tool_use_calls == 1
    assert handler.post_tool_use_failure_calls == 0


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_post_tool_use_failure_parity(backend: str) -> None:
    """is_error=True must route to on_post_tool_use_failure on BOTH backends,
    and on_post_tool_use must NOT be called."""
    registry = HookRegistry()
    handler = CountingHandler()
    registry.register(handler)

    if backend == "claude":
        cb = _claude_sdk_hooks(registry)["PostToolUse"]
        result = await cb(
            {
                "tool_name": "Bash",
                "tool_input": {"cmd": "false"},
                "tool_response": {"content": [{"text": "failed"}], "isError": True},
            },
            "id-1",
            None,
        )
        assert result == {}
    else:
        from pi.ai.types import TextContent
        from pi.coding_agent.core.extensions.types import (
            ToolResultEvent as PiEvt,
        )

        cb = _pi_bridge_handlers(registry)["tool_result"]
        result = await cb(
            PiEvt(
                tool_name="bash",
                tool_call_id="id-1",
                input={"cmd": "false"},
                content=[TextContent(text="failed")],
                is_error=True,
            ),
            None,
        )
        assert result is None

    assert handler.post_tool_use_calls == 0
    assert handler.post_tool_use_failure_calls == 1


# ---------------------------------------------------------------------------
# Claude-only: modified_input honored; Pi: degrades with warning
# ---------------------------------------------------------------------------


async def test_pre_tool_use_modified_input_claude_honored() -> None:
    """Claude's bridge must surface updatedInput in the SDK format."""
    registry = HookRegistry()

    class Modifier:
        async def on_pre_tool_use(
            self, event: ToolCallEvent
        ) -> ToolCallVerdict | None:
            return ToolCallVerdict(modified_input={"cmd": "ls -la /safe"})

    registry.register(Modifier())
    cb = _claude_sdk_hooks(registry)["PreToolUse"]
    result = await cb(
        {"tool_name": "Bash", "tool_input": {"cmd": "ls /etc"}},
        "id-1",
        None,
    )
    assert (
        result["hookSpecificOutput"]["updatedInput"] == {"cmd": "ls -la /safe"}
    )


async def test_pre_tool_use_modified_input_pi_degrades(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pi's ToolCallEventResult has no input-modification slot. The bridge
    logs a warning and returns None so the tool proceeds with the original
    input. This is a documented asymmetry — we assert it stays documented
    in behavior."""
    from pi.coding_agent.core.extensions.types import ToolCallEvent as PiEvt

    registry = HookRegistry()

    class Modifier:
        async def on_pre_tool_use(
            self, event: ToolCallEvent
        ) -> ToolCallVerdict | None:
            return ToolCallVerdict(modified_input={"cmd": "ls /safe"})

    registry.register(Modifier())
    cb = _pi_bridge_handlers(registry)["tool_call"]
    with caplog.at_level("WARNING", logger="agent_runner.pi_backend"):
        result = await cb(
            PiEvt(tool_name="bash", tool_call_id="id-1", input={"cmd": "ls /etc"}),
            None,
        )
    assert result is None
    assert any(
        "modified_input not supported" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Parity: UserPromptSubmit (block & allow)
# ---------------------------------------------------------------------------


async def test_user_prompt_submit_block_claude() -> None:
    registry = HookRegistry()
    registry.register(CountingHandler(block_prompt=True))
    cb = _claude_sdk_hooks(registry)["UserPromptSubmit"]
    result = await cb({"prompt": "FORBIDDEN"}, None, None)
    # Claude SDK's block uses top-level decision, NOT hookSpecificOutput
    assert result["decision"] == "block"
    assert result["reason"] == "prompt forbidden"


async def test_user_prompt_submit_allow_claude_returns_empty() -> None:
    registry = HookRegistry()
    registry.register(CountingHandler(block_prompt=False))
    cb = _claude_sdk_hooks(registry)["UserPromptSubmit"]
    result = await cb({"prompt": "hello"}, None, None)
    assert result == {}


async def test_user_prompt_submit_block_pi() -> None:
    """Pi does not use an extension callback for UserPromptSubmit; the bridge
    invokes emit_user_prompt_submit from _apply_user_prompt_hook directly.
    When a handler blocks, the method returns None (meaning 'do not call
    session.prompt'). This is the critical guarantee — a drift here would
    allow a blocked prompt to reach the agent."""
    registry = HookRegistry()
    registry.register(CountingHandler(block_prompt=True))

    backend = pi_backend.PiBackend()
    backend._hooks = registry
    # Capture the block message surfaced to the UI.
    emitted: list[Any] = []

    async def listener(event: Any) -> None:
        emitted.append(event)

    backend.subscribe(listener)

    result = await backend._apply_user_prompt_hook("FORBIDDEN")
    assert result is None
    assert len(emitted) == 1
    assert "prompt forbidden" in emitted[0].text


async def test_user_prompt_submit_allow_pi_returns_text_unchanged() -> None:
    """When no handler blocks or appends, _apply_user_prompt_hook returns
    the original prompt text verbatim."""
    registry = HookRegistry()
    registry.register(CountingHandler(block_prompt=False))
    backend = pi_backend.PiBackend()
    backend._hooks = registry

    async def listener(event: Any) -> None:
        pass

    backend.subscribe(listener)

    result = await backend._apply_user_prompt_hook("hello")
    assert result == "hello"


# ---------------------------------------------------------------------------
# Parity: PreCompact visits the handler on both bridges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_pre_compact_parity(backend: str) -> None:
    registry = HookRegistry()
    handler = CountingHandler()
    registry.register(handler)

    if backend == "claude":
        cb = _claude_sdk_hooks(registry)["PreCompact"]
        await cb(
            {"transcript_path": "/tmp/does-not-exist.jsonl", "session_id": "s"},
            None,
            None,
        )
    else:
        cb = _pi_bridge_handlers(registry)["session_before_compact"]

        class _Prep:
            def __init__(self) -> None:
                self.messages_to_summarize: list[Any] = []

        await cb({"type": "session_before_compact", "preparation": _Prep()}, None)

    assert handler.pre_compact_calls == 1
