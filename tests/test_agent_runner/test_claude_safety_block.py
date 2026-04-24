"""Prove UserPromptSubmit block emits a SafetyBlockEvent on Claude.

Regression target — docs/safety/toggle-experiments.md §8:

Claude SDK's ``UserPromptSubmit`` hook returning ``{"decision":"block"}``
short-circuits the turn WITHOUT yielding any message. The backend's
consume loop iterates zero times, so no event is emitted, so the
orchestrator publishes nothing, so the browser UI hangs in "thinking..."
for ~2 minutes until the watchdog fires.

These tests drive ``_build_hook_callbacks``'s user_prompt_submit
callback directly and assert:

  1. ``emit_safety_block`` is called with a SafetyBlockEvent carrying
     ``stage='input_prompt'`` and the verdict reason.
  2. The returned SDK response still carries ``decision=block`` — the
     telemetry emission must not accidentally flip safety-block to
     safety-allow.
  3. Non-block verdicts do NOT invoke emit_safety_block (the zero-cost
     contract for the common allow path).
  4. A raising emit_safety_block still leaves the block decision intact
     (fail-safe: telemetry failure ≠ safety failure).
  5. Back-compat: legacy callers without the emit_safety_block kwarg
     still produce a working block response.
"""

from __future__ import annotations

import sys
import types
from typing import Any

# Stub claude_agent_sdk BEFORE importing claude_backend.
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
from agent_runner.backend import SafetyBlockEvent  # noqa: E402
from agent_runner.hooks import HookRegistry, UserPromptEvent, UserPromptVerdict  # noqa: E402


class _FixedHookMatcher:
    def __init__(self, hooks: list[Any] | None = None, **_kw: Any) -> None:
        self.hooks = list(hooks) if hooks else []


claude_backend.HookMatcher = _FixedHookMatcher  # type: ignore[assignment]


class _BlockingPromptHook:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def on_user_prompt_submit(
        self, _event: UserPromptEvent
    ) -> UserPromptVerdict:
        return UserPromptVerdict(block=True, reason=self._reason)


class _AllowingPromptHook:
    async def on_user_prompt_submit(
        self, _event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        return None


def _user_prompt_cb(
    hook: Any,
    emit_safety_block: Any = None,
) -> Any:
    reg = HookRegistry()
    reg.register(hook)
    matchers = claude_backend._build_hook_callbacks(
        reg, emit_safety_block=emit_safety_block
    )
    return matchers["UserPromptSubmit"][0].hooks[0]


async def test_block_emits_safety_block_event_with_correct_stage() -> None:
    emitted: list[SafetyBlockEvent] = []

    async def record(event: SafetyBlockEvent) -> None:
        emitted.append(event)

    cb = _user_prompt_cb(
        _BlockingPromptHook("Blocked: detected PII.PHONE_US"),
        emit_safety_block=record,
    )

    result = await cb({"prompt": "555-1234"}, None, None)

    # Safety intent preserved: SDK sees block.
    assert result["decision"] == "block"
    assert result["reason"] == "Blocked: detected PII.PHONE_US"
    # UI-visibility: exactly one SafetyBlockEvent with the correct shape.
    assert len(emitted) == 1
    event = emitted[0]
    assert event.stage == "input_prompt"
    assert event.reason == "Blocked: detected PII.PHONE_US"
    # rule_id defaults to None — pipeline aggregate doesn't propagate it;
    # audit side persists rule_id separately in safety_decisions.
    assert event.rule_id is None


async def test_block_without_explicit_reason_uses_default_in_both_places() -> None:
    """Fallback string is IDENTICAL on the SDK response and the
    SafetyBlockEvent — downstream audit and user-visible text agree.
    """
    emitted: list[SafetyBlockEvent] = []

    async def record(event: SafetyBlockEvent) -> None:
        emitted.append(event)

    class _BlockNoReason:
        async def on_user_prompt_submit(
            self, _event: UserPromptEvent
        ) -> UserPromptVerdict:
            return UserPromptVerdict(block=True)

    cb = _user_prompt_cb(_BlockNoReason(), emit_safety_block=record)
    result = await cb({"prompt": "hi"}, None, None)

    assert result["reason"] == "Prompt blocked by hook"
    assert len(emitted) == 1
    assert emitted[0].reason == "Prompt blocked by hook"


async def test_allow_does_not_invoke_emit_safety_block() -> None:
    """Zero-overhead for the common case: allow → no event."""
    emitted: list[SafetyBlockEvent] = []

    async def record(event: SafetyBlockEvent) -> None:
        emitted.append(event)

    cb = _user_prompt_cb(_AllowingPromptHook(), emit_safety_block=record)
    result = await cb({"prompt": "hi"}, None, None)

    assert result == {}
    assert emitted == []


async def test_emit_raising_does_not_downgrade_to_allow() -> None:
    """Fail-safe: a telemetry emit failure must NOT convert safety-block
    into safety-allow. If the synthesized event fails to publish
    (broken listener, closed WebSocket, etc.), the SDK still receives
    decision=block — otherwise a downstream infra blip would invert
    the entire hook's purpose.
    """
    async def raising(_: SafetyBlockEvent) -> None:
        raise RuntimeError("synthetic telemetry failure")

    cb = _user_prompt_cb(
        _BlockingPromptHook("stop"),
        emit_safety_block=raising,
    )

    result = await cb({"prompt": "anything"}, None, None)

    assert result["decision"] == "block"
    assert result["reason"] == "stop"


async def test_legacy_caller_without_emit_still_blocks() -> None:
    """Back-compat: _build_hook_callbacks(hooks) without the new kwarg
    still produces a correct block response (just without the
    synthesized event — the silent-hang regression returns for that
    configuration, but the decision semantics are preserved)."""
    cb = _user_prompt_cb(_BlockingPromptHook("legacy"))  # no kwarg

    result = await cb({"prompt": "x"}, None, None)

    assert result == {"decision": "block", "reason": "legacy"}
