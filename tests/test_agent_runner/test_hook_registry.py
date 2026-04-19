"""HookRegistry behavior tests.

Focus: cases that mutation-test the dispatch logic rather than mirror it.
The tests below are designed to fail on these mutations:

  - Reordering: iterating handlers in reverse would break block short-circuit
  - Off-by-one: a handler that raises but whose exception is swallowed
  - Silent identity: returning the original verdict when we should merge
  - try/except in the wrong place: fail-close turned into fail-safe
  - None-coalesce collapses: treating None as "empty" vs "not called"
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_runner.hooks import (
    CompactionEvent,
    HookRegistry,
    StopEvent,
    ToolCallEvent,
    ToolCallVerdict,
    ToolResultEvent,
    ToolResultVerdict,
    UserPromptEvent,
    UserPromptVerdict,
)

# ---------------------------------------------------------------------------
# Helper handlers
# ---------------------------------------------------------------------------


@dataclass
class RecordingHandler:
    """Records every event it sees; methods configurable per-test."""

    pre_tool_use_verdict: ToolCallVerdict | None = None
    post_tool_use_verdict: ToolResultVerdict | None = None
    user_prompt_verdict: UserPromptVerdict | None = None
    pre_tool_use_raises: Exception | None = None
    post_tool_use_raises: Exception | None = None
    user_prompt_raises: Exception | None = None
    pre_compact_raises: Exception | None = None
    stop_raises: Exception | None = None
    pre_tool_use_seen: list[ToolCallEvent] = field(default_factory=list)
    post_tool_use_seen: list[ToolResultEvent] = field(default_factory=list)
    post_tool_use_failure_seen: list[ToolResultEvent] = field(default_factory=list)
    pre_compact_seen: list[CompactionEvent] = field(default_factory=list)
    user_prompt_seen: list[UserPromptEvent] = field(default_factory=list)
    stop_seen: list[StopEvent] = field(default_factory=list)

    async def on_pre_tool_use(self, event: ToolCallEvent) -> ToolCallVerdict | None:
        self.pre_tool_use_seen.append(event)
        if self.pre_tool_use_raises is not None:
            raise self.pre_tool_use_raises
        return self.pre_tool_use_verdict

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        self.post_tool_use_seen.append(event)
        if self.post_tool_use_raises is not None:
            raise self.post_tool_use_raises
        return self.post_tool_use_verdict

    async def on_post_tool_use_failure(self, event: ToolResultEvent) -> None:
        self.post_tool_use_failure_seen.append(event)

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        self.pre_compact_seen.append(event)
        if self.pre_compact_raises is not None:
            raise self.pre_compact_raises

    async def on_user_prompt_submit(
        self, event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        self.user_prompt_seen.append(event)
        if self.user_prompt_raises is not None:
            raise self.user_prompt_raises
        return self.user_prompt_verdict

    async def on_stop(self, event: StopEvent) -> None:
        self.stop_seen.append(event)
        if self.stop_raises is not None:
            raise self.stop_raises


class PartialHandler:
    """Only implements on_pre_compact — registry must tolerate missing methods."""

    def __init__(self) -> None:
        self.compact_count = 0

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        self.compact_count += 1


# ---------------------------------------------------------------------------
# Empty registry
# ---------------------------------------------------------------------------


async def test_empty_registry_returns_none_for_control_hooks() -> None:
    r = HookRegistry()
    assert await r.emit_pre_tool_use(ToolCallEvent("t", {})) is None
    assert await r.emit_user_prompt_submit(UserPromptEvent("hi")) is None
    assert await r.emit_post_tool_use(ToolResultEvent("t", {}, "ok")) is None
    # Observational hooks don't return anything; ensure they don't raise.
    await r.emit_post_tool_use_failure(ToolResultEvent("t", {}, "", is_error=True))
    await r.emit_pre_compact(CompactionEvent())
    await r.emit_stop(StopEvent(reason="completed"))


async def test_empty_registry_is_falsy() -> None:
    r = HookRegistry()
    assert not r
    r.register(RecordingHandler())
    assert r


# ---------------------------------------------------------------------------
# PreToolUse — control hook semantics
# ---------------------------------------------------------------------------


async def test_pre_tool_use_no_verdicts_returns_none() -> None:
    """Handlers that return None from all probes -> emit returns None.
    Mutation target: returning a default ToolCallVerdict() collapses this.
    """
    r = HookRegistry()
    r.register(RecordingHandler())
    r.register(RecordingHandler())
    out = await r.emit_pre_tool_use(ToolCallEvent("bash", {"cmd": "ls"}))
    assert out is None


async def test_pre_tool_use_block_short_circuits() -> None:
    """First block must short-circuit; second handler must NOT be probed.
    Mutation target: iterating handlers after a block arrived.
    """
    r = HookRegistry()
    h1 = RecordingHandler(pre_tool_use_verdict=ToolCallVerdict(block=True, reason="nope"))
    h2 = RecordingHandler()
    r.register(h1)
    r.register(h2)
    verdict = await r.emit_pre_tool_use(ToolCallEvent("bash", {"cmd": "ls"}))
    assert verdict is not None
    assert verdict.block is True
    assert verdict.reason == "nope"
    assert len(h1.pre_tool_use_seen) == 1
    assert h2.pre_tool_use_seen == []


async def test_pre_tool_use_modified_input_chains() -> None:
    """H1 rewrites input -> H2 must see the rewritten input, not the original.
    Mutation target: the registry reusing the caller's tool_input for every
    handler instead of feeding the latest version forward.
    """
    r = HookRegistry()
    h1 = RecordingHandler(
        pre_tool_use_verdict=ToolCallVerdict(modified_input={"cmd": "ls -la"})
    )
    h2 = RecordingHandler()  # just observes; no verdict
    r.register(h1)
    r.register(h2)
    verdict = await r.emit_pre_tool_use(ToolCallEvent("bash", {"cmd": "ls"}))
    assert verdict is not None
    assert verdict.block is False
    assert verdict.modified_input == {"cmd": "ls -la"}
    assert h2.pre_tool_use_seen[0].tool_input == {"cmd": "ls -la"}


async def test_pre_tool_use_failclose_propagates() -> None:
    """Handler exception in PreToolUse MUST propagate (fail-close).
    Mutation target: a try/except around the inner loop would silently swallow
    the exception and the agent would execute the tool unrestricted.
    """
    r = HookRegistry()
    r.register(RecordingHandler(pre_tool_use_raises=RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="db down"):
        await r.emit_pre_tool_use(ToolCallEvent("bash", {}))


async def test_pre_tool_use_first_handler_crashes_prevents_second_call() -> None:
    """Fail-close means subsequent handlers are NOT called either — once one
    raises we abort the whole emit. Mutation target: continuing to probe h2
    after h1 raised."""
    r = HookRegistry()
    h1 = RecordingHandler(pre_tool_use_raises=RuntimeError("boom"))
    h2 = RecordingHandler()
    r.register(h1)
    r.register(h2)
    with pytest.raises(RuntimeError, match="boom"):
        await r.emit_pre_tool_use(ToolCallEvent("t", {}))
    assert h2.pre_tool_use_seen == []


# ---------------------------------------------------------------------------
# PostToolUse — observational hook semantics
# ---------------------------------------------------------------------------


async def test_post_tool_use_appends_concatenate_with_two_newlines() -> None:
    """Two handlers both return appended_context; merged with exactly one
    blank-line separator. Mutation target: using "\n" instead of "\n\n"
    would break expected paragraph spacing in the agent's view."""
    r = HookRegistry()
    r.register(RecordingHandler(post_tool_use_verdict=ToolResultVerdict("AUDIT-1")))
    r.register(RecordingHandler(post_tool_use_verdict=ToolResultVerdict("AUDIT-2")))
    verdict = await r.emit_post_tool_use(ToolResultEvent("t", {}, "result"))
    assert verdict is not None
    assert verdict.appended_context == "AUDIT-1\n\nAUDIT-2"


async def test_post_tool_use_handler_crash_does_not_abort_others() -> None:
    """H1 raises -> H2 still invoked. Fail-safe.
    Mutation target: the try/except that wraps each handler individually — if
    someone moved it outside the loop the test fails because h2 never runs."""
    r = HookRegistry()
    h1 = RecordingHandler(post_tool_use_raises=RuntimeError("audit write failed"))
    h2 = RecordingHandler(post_tool_use_verdict=ToolResultVerdict("SURVIVED"))
    r.register(h1)
    r.register(h2)
    verdict = await r.emit_post_tool_use(ToolResultEvent("t", {}, "result"))
    assert verdict is not None
    assert verdict.appended_context == "SURVIVED"


async def test_post_tool_use_only_empty_verdicts_returns_none() -> None:
    """Both handlers ack but neither appends -> emit returns None.
    Mutation target: returning an empty ToolResultVerdict() would trigger
    bridges to attach a zero-length additionalContext."""
    r = HookRegistry()
    r.register(RecordingHandler())
    r.register(RecordingHandler(post_tool_use_verdict=ToolResultVerdict()))
    out = await r.emit_post_tool_use(ToolResultEvent("t", {}, "result"))
    assert out is None


# ---------------------------------------------------------------------------
# PostToolUseFailure
# ---------------------------------------------------------------------------


async def test_post_tool_use_failure_visits_all_handlers() -> None:
    r = HookRegistry()
    h1 = RecordingHandler()
    h2 = RecordingHandler()
    r.register(h1)
    r.register(h2)
    evt = ToolResultEvent("bash", {"cmd": "x"}, "boom", is_error=True)
    await r.emit_post_tool_use_failure(evt)
    assert len(h1.post_tool_use_failure_seen) == 1
    assert len(h2.post_tool_use_failure_seen) == 1
    assert h1.post_tool_use_failure_seen[0] is evt


# ---------------------------------------------------------------------------
# PreCompact
# ---------------------------------------------------------------------------


async def test_pre_compact_handler_crash_isolated() -> None:
    r = HookRegistry()
    h1 = RecordingHandler(pre_compact_raises=RuntimeError("disk full"))
    h2 = RecordingHandler()
    r.register(h1)
    r.register(h2)
    # Must not raise.
    await r.emit_pre_compact(CompactionEvent(session_id="abc"))
    # h2 was still visited.
    assert len(h2.pre_compact_seen) == 1


async def test_partial_handler_only_compact() -> None:
    """Handler implementing only on_pre_compact must be tolerated by every
    emit_* method. Mutation target: forgetting getattr()-based dispatch would
    cause AttributeError on the other hooks."""
    r = HookRegistry()
    r.register(PartialHandler())
    # None of these should raise.
    assert await r.emit_pre_tool_use(ToolCallEvent("t", {})) is None
    assert await r.emit_post_tool_use(ToolResultEvent("t", {}, "")) is None
    await r.emit_post_tool_use_failure(ToolResultEvent("t", {}, "", is_error=True))
    assert await r.emit_user_prompt_submit(UserPromptEvent("q")) is None
    await r.emit_stop(StopEvent(reason="completed"))
    await r.emit_pre_compact(CompactionEvent())


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------


async def test_user_prompt_submit_block_short_circuits() -> None:
    r = HookRegistry()
    h1 = RecordingHandler(
        user_prompt_verdict=UserPromptVerdict(block=True, reason="banned")
    )
    h2 = RecordingHandler()
    r.register(h1)
    r.register(h2)
    verdict = await r.emit_user_prompt_submit(UserPromptEvent("secret"))
    assert verdict is not None
    assert verdict.block is True
    assert verdict.reason == "banned"
    assert h2.user_prompt_seen == []


async def test_user_prompt_submit_failclose_propagates() -> None:
    r = HookRegistry()
    r.register(RecordingHandler(user_prompt_raises=RuntimeError("validator crash")))
    with pytest.raises(RuntimeError, match="validator crash"):
        await r.emit_user_prompt_submit(UserPromptEvent("hi"))


async def test_user_prompt_submit_append_context_merges() -> None:
    r = HookRegistry()
    r.register(
        RecordingHandler(user_prompt_verdict=UserPromptVerdict(appended_context="CTX1"))
    )
    r.register(
        RecordingHandler(user_prompt_verdict=UserPromptVerdict(appended_context="CTX2"))
    )
    verdict = await r.emit_user_prompt_submit(UserPromptEvent("q"))
    assert verdict is not None
    assert verdict.appended_context == "CTX1\n\nCTX2"
    assert verdict.block is False


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def test_stop_handler_crash_isolated() -> None:
    r = HookRegistry()
    h1 = RecordingHandler(stop_raises=RuntimeError("metrics sink down"))
    h2 = RecordingHandler()
    r.register(h1)
    r.register(h2)
    # Must not raise.
    await r.emit_stop(StopEvent(reason="aborted"))
    assert len(h2.stop_seen) == 1
    assert h2.stop_seen[0].reason == "aborted"


async def test_stop_event_carries_reason_and_session_id() -> None:
    r = HookRegistry()
    h = RecordingHandler()
    r.register(h)
    await r.emit_stop(StopEvent(reason="completed", session_id="sess-42"))
    assert h.stop_seen[0].reason == "completed"
    assert h.stop_seen[0].session_id == "sess-42"
