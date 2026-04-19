"""End-to-end UserPromptSubmit coverage across both backends.

Closes three gaps left by test_hook_parity.py:

  1. Pi: append_context actually prefixes the agent's prompt — parity
     tests only validated block/allow, not the "modify outgoing text"
     path. A regression (e.g. the bridge forgetting to join with
     "\n\n") would produce prompts like "CTXhi" and nobody would
     notice until a user's CLAUDE.md flag reached the model.

  2. Pi: follow-up messages also run through the hook (spec §3.6 —
     "we emit hooks.emit_user_prompt_submit() directly from
     run_prompt() and handle_follow_up()"). If handle_follow_up
     forgets, follow-up prompts bypass the audit/DLP handler entirely.

  3. Pi: multi-handler chaining — multiple handlers' appended_context
     all show up in the final prompt, joined with blank lines. Parity
     tests use a single handler; chaining is only tested at the
     Registry unit level.

Also re-verifies the Claude side's append path end-to-end, since the
parity suite only covered block/allow there as well.
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


from agent_runner import claude_backend, pi_backend  # noqa: E402


class _RecordingHookMatcher:
    def __init__(self, hooks: list[Any] | None = None, **_kw: Any) -> None:
        self.hooks = list(hooks) if hooks else []


claude_backend.HookMatcher = _RecordingHookMatcher  # type: ignore[assignment]


from agent_runner.hooks import (  # noqa: E402
    HookRegistry,
    UserPromptEvent,
    UserPromptVerdict,
)

# ---------------------------------------------------------------------------
# Pi side: _apply_user_prompt_hook end-to-end
# ---------------------------------------------------------------------------


class _PromptRecorder:
    def __init__(self, append: str | None = None) -> None:
        self._append = append
        self.seen: list[str] = []

    async def on_user_prompt_submit(
        self, event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        self.seen.append(event.prompt)
        if self._append is not None:
            return UserPromptVerdict(appended_context=self._append)
        return None


def _make_pi_backend(registry: HookRegistry) -> pi_backend.PiBackend:
    backend = pi_backend.PiBackend()
    backend._hooks = registry
    backend._session_file = "sid"
    return backend


async def test_pi_append_context_prefixed_with_blank_line() -> None:
    """Single handler appends; the final prompt seen by session.prompt
    must be `<appended>\\n\\n<original>`. Whitespace between the two
    matters — without the blank-line boundary the agent sees the
    context glued to the user's first word."""
    registry = HookRegistry()
    recorder = _PromptRecorder(append="CTX")
    registry.register(recorder)
    backend = _make_pi_backend(registry)

    result = await backend._apply_user_prompt_hook("hello")

    assert result == "CTX\n\nhello"
    assert recorder.seen == ["hello"]  # original was what the handler observed


async def test_pi_append_context_multi_handler_joined_with_blank_line() -> None:
    """Two handlers both append — the final prompt prefix is
    'A\\n\\nB'. Mutation target: a bridge that joins with '\\n'
    produces 'A\\nB' and the two contexts blur together."""
    registry = HookRegistry()
    registry.register(_PromptRecorder(append="A"))
    registry.register(_PromptRecorder(append="B"))
    backend = _make_pi_backend(registry)

    result = await backend._apply_user_prompt_hook("payload")

    # Registry joins the two appends with \n\n first, then the bridge
    # prefixes that onto the prompt with another \n\n separator.
    assert result == "A\n\nB\n\npayload"


async def test_pi_allow_passes_prompt_unchanged() -> None:
    """Single handler that returns None verdict -> prompt verbatim."""
    registry = HookRegistry()
    registry.register(_PromptRecorder(append=None))
    backend = _make_pi_backend(registry)

    result = await backend._apply_user_prompt_hook("hello")

    assert result == "hello"


async def test_pi_block_emits_result_event_with_reason() -> None:
    """Blocking prompt -> _apply_user_prompt_hook returns None AND
    fires one ResultEvent whose text carries the handler's reason. The
    orchestrator's UI shows this as the reply bubble so the user
    understands why their message wasn't answered."""

    class _Blocker:
        async def on_user_prompt_submit(
            self, event: UserPromptEvent
        ) -> UserPromptVerdict | None:
            return UserPromptVerdict(block=True, reason="off-hours")

    registry = HookRegistry()
    registry.register(_Blocker())
    backend = _make_pi_backend(registry)
    emitted: list[Any] = []

    async def listener(event: Any) -> None:
        emitted.append(event)

    backend.subscribe(listener)

    result = await backend._apply_user_prompt_hook("some question")

    assert result is None
    # Exactly one ResultEvent carrying the block reason
    assert len(emitted) == 1
    assert "off-hours" in emitted[0].text


async def test_pi_hook_crash_produces_error_result_event() -> None:
    """Fail-close for Pi's direct emit path: handler raises ->
    _apply_user_prompt_hook returns None (do not call session.prompt)
    AND surfaces 'Hook system error' to the UI. Mutation: if the
    bridge swallowed the error and returned the original prompt,
    a broken DLP validator would silently stop validating."""

    class _Crasher:
        async def on_user_prompt_submit(
            self, event: UserPromptEvent
        ) -> UserPromptVerdict | None:
            raise RuntimeError("validator crashed")

    registry = HookRegistry()
    registry.register(_Crasher())
    backend = _make_pi_backend(registry)
    emitted: list[Any] = []

    async def listener(event: Any) -> None:
        emitted.append(event)

    backend.subscribe(listener)

    result = await backend._apply_user_prompt_hook("payload")

    assert result is None
    assert len(emitted) == 1
    assert "Hook system error" in emitted[0].text
    assert "validator crashed" in emitted[0].text


# ---------------------------------------------------------------------------
# Pi: follow-up path also runs through the hook
# ---------------------------------------------------------------------------


async def test_pi_follow_up_blocked_by_user_prompt_submit() -> None:
    """handle_follow_up must also invoke the UserPromptSubmit hook.
    Spec §3.6: 'we emit hooks.emit_user_prompt_submit() directly from
    run_prompt() and handle_follow_up()'. If a regression drops it,
    follow-up messages bypass audit/DLP validation entirely."""
    blocked: list[str] = []

    class _Gatekeeper:
        async def on_user_prompt_submit(
            self, event: UserPromptEvent
        ) -> UserPromptVerdict | None:
            blocked.append(event.prompt)
            return UserPromptVerdict(block=True, reason="nope")

    registry = HookRegistry()
    registry.register(_Gatekeeper())
    backend = _make_pi_backend(registry)
    backend._aborting = False

    # Stub session so handle_follow_up can reach _apply_user_prompt_hook
    class _Session:
        is_streaming = False

        def __init__(self) -> None:
            self.prompt_calls: list[str] = []

        async def prompt(self, text: str, **_kw: Any) -> None:
            self.prompt_calls.append(text)

    session = _Session()
    backend._session = session  # type: ignore[assignment]

    await backend.handle_follow_up("late message")

    assert blocked == ["late message"]
    assert session.prompt_calls == [], (
        "follow-up must not reach session.prompt when blocked"
    )


async def test_pi_follow_up_append_context_prefixes_the_text() -> None:
    """Follow-up also gets the append treatment — the appended context
    is prefixed onto the follow-up text before it reaches the session.
    Validates that the same transformation applies consistently to
    initial prompts and follow-ups."""

    class _Appender:
        async def on_user_prompt_submit(
            self, event: UserPromptEvent
        ) -> UserPromptVerdict | None:
            return UserPromptVerdict(appended_context="AUDIT")

    registry = HookRegistry()
    registry.register(_Appender())
    backend = _make_pi_backend(registry)
    backend._aborting = False

    class _Session:
        is_streaming = False

        def __init__(self) -> None:
            self.prompt_calls: list[str] = []

        async def prompt(self, text: str, **_kw: Any) -> None:
            self.prompt_calls.append(text)

    session = _Session()
    backend._session = session  # type: ignore[assignment]

    await backend.handle_follow_up("follow-up question")

    assert session.prompt_calls == ["AUDIT\n\nfollow-up question"]


# ---------------------------------------------------------------------------
# Claude side: append path
# ---------------------------------------------------------------------------


def _claude_callbacks(registry: HookRegistry) -> dict[str, Any]:
    matchers = claude_backend._build_hook_callbacks(registry)
    return {event: matchers[event][0].hooks[0] for event in matchers}


async def test_claude_append_returns_additional_context() -> None:
    """SDK-shaped response for append: top-level 'hookSpecificOutput'
    with 'additionalContext'. Parity test covered only block;
    Claude-specific append wire format is distinct from Pi's and
    deserves its own check."""
    registry = HookRegistry()
    registry.register(_PromptRecorder(append="POLICY"))
    cb = _claude_callbacks(registry)["UserPromptSubmit"]

    result = await cb({"prompt": "hi"}, None, None)

    assert result == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "POLICY",
        }
    }


async def test_claude_append_multi_handler_joined() -> None:
    registry = HookRegistry()
    registry.register(_PromptRecorder(append="A"))
    registry.register(_PromptRecorder(append="B"))
    cb = _claude_callbacks(registry)["UserPromptSubmit"]

    result = await cb({"prompt": "hi"}, None, None)

    assert (
        result["hookSpecificOutput"]["additionalContext"] == "A\n\nB"
    )


async def test_claude_hook_crash_returns_block_with_error() -> None:
    """Fail-close translated to the SDK's block shape (top-level
    'decision', not hookSpecificOutput)."""

    class _Crasher:
        async def on_user_prompt_submit(
            self, event: UserPromptEvent
        ) -> UserPromptVerdict | None:
            raise RuntimeError("auth failure")

    registry = HookRegistry()
    registry.register(_Crasher())
    cb = _claude_callbacks(registry)["UserPromptSubmit"]

    result = await cb({"prompt": "secret"}, None, None)

    assert result["decision"] == "block"
    assert "Hook system error" in result["reason"]
    assert "auth failure" in result["reason"]
