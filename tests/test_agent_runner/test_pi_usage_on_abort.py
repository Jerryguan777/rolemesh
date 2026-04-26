"""Pi backend: abort + error paths must not lose accumulated tokens.

Provider already billed for tokens streamed before an abort or upstream
failure. Dropping them here would systematically under-report cost.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_runner.backend import (
    BackendEvent,
    ErrorEvent,
    ResultEvent,
    StoppedEvent,
)
from agent_runner.pi_backend import PiBackend
from pi.agent.types import (
    MessageEndEvent,
    PromptTurnCompleteEvent,
)
from pi.ai.types import AssistantMessage, Usage


def _msg(input_t: int, output_t: int, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        model="claude-sonnet-4-6",
        usage=Usage(input=input_t, output=output_t),
        stop_reason=stop_reason,  # type: ignore[arg-type]
    )


class _CapturingListener:
    """Capture every BackendEvent emitted; expose as a flat list."""

    def __init__(self) -> None:
        self.events: list[BackendEvent] = []

    async def __call__(self, event: BackendEvent) -> None:
        self.events.append(event)


async def _drain(events: list[asyncio.Task[Any]]) -> None:
    """Let scheduled emit tasks land before assertions."""
    # _handle_event uses _schedule_emit which spawns tasks; pending tasks
    # in PiBackend._bg_tasks are what carry the actual emits to the
    # listener. A small sleep is enough to let them run in cooperative
    # scheduling — far cheaper than gathering on private state.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestPiAbortPreservesTokens:
    async def test_abort_emits_stopped_with_acc_tokens(self) -> None:
        """Abort fires after a few message_end events. The StoppedEvent
        emitted by abort() carries the partial-turn UsageSnapshot, and
        the accumulator is reset so the next prompt starts clean."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        # Simulate two LLM calls completed before the user hits Stop.
        backend._handle_event(MessageEndEvent(message=_msg(100, 50)))
        backend._handle_event(MessageEndEvent(message=_msg(200, 75)))

        # Drain the scheduled emits (none expected for MessageEnd, but
        # cleaner to sync).
        await _drain([])

        # No active session — abort goes through the "no session"
        # branch and just emits the StoppedEvent.
        await backend.abort()

        stopped_events = [e for e in listener.events if isinstance(e, StoppedEvent)]
        assert len(stopped_events) == 1
        snap = stopped_events[0].usage
        assert snap is not None
        # Captured tokens from BOTH message_end events.
        assert snap.input_tokens == 300
        assert snap.output_tokens == 125
        # And the acc must be reset — a follow-up prompt that adds
        # more tokens won't double-count.
        assert backend._usage_acc.is_empty()

    async def test_abort_with_no_tokens_yields_stopped_with_none_usage(self) -> None:
        """Abort before any LLM call — usage is None, not a zero
        snapshot, so analytics can filter out 'aborts that never
        called the model'."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        await backend.abort()

        stopped_events = [e for e in listener.events if isinstance(e, StoppedEvent)]
        assert len(stopped_events) == 1
        assert stopped_events[0].usage is None


class TestPiPromptCompletionResets:
    async def test_prompt_complete_emits_result_with_snapshot(self) -> None:
        """The "happy path" — message_end events accumulate, then
        PromptTurnCompleteEvent flushes them onto the ResultEvent."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        backend._handle_event(MessageEndEvent(message=_msg(100, 50)))
        backend._handle_event(MessageEndEvent(message=_msg(50, 25)))

        # Final assistant message that the prompt-completion event
        # would carry. stop_reason="stop" is the success path.
        final = AssistantMessage(
            model="claude-sonnet-4-6",
            content=[],
            stop_reason="stop",
        )
        # Stub in some text so the ResultEvent emit path fires.
        from pi.ai.types import TextContent

        final.content = [TextContent(text="hello")]
        backend._handle_event(PromptTurnCompleteEvent(message=final))

        await _drain([])

        result_events = [e for e in listener.events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        snap = result_events[0].usage
        assert snap is not None
        assert snap.input_tokens == 150
        assert snap.output_tokens == 75
        # And the acc was reset for the next prompt.
        assert backend._usage_acc.is_empty()

    async def test_two_prompts_dont_share_accumulator(self) -> None:
        """The cross-prompt isolation contract: prompt 1 shouldn't
        contribute to prompt 2's snapshot. Most likely failure mode if
        reset is missed."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        from pi.ai.types import TextContent

        # Prompt 1 — 100/50 tokens.
        backend._handle_event(MessageEndEvent(message=_msg(100, 50)))
        first = AssistantMessage(model="claude-sonnet-4-6", stop_reason="stop")
        first.content = [TextContent(text="a")]
        backend._handle_event(PromptTurnCompleteEvent(message=first))

        # Prompt 2 — 30/20 tokens.
        backend._handle_event(MessageEndEvent(message=_msg(30, 20)))
        second = AssistantMessage(model="claude-sonnet-4-6", stop_reason="stop")
        second.content = [TextContent(text="b")]
        backend._handle_event(PromptTurnCompleteEvent(message=second))

        await _drain([])

        result_events = [e for e in listener.events if isinstance(e, ResultEvent)]
        assert len(result_events) == 2
        # Prompt 1 sees only its own tokens.
        assert result_events[0].usage is not None
        assert result_events[0].usage.input_tokens == 100
        # Prompt 2 sees ONLY its own — not 100+30.
        assert result_events[1].usage is not None
        assert result_events[1].usage.input_tokens == 30
        assert result_events[1].usage.output_tokens == 20


class TestPiErrorPathPreservesTokens:
    async def test_error_stop_reason_emits_error_with_snapshot(self) -> None:
        """When PromptTurnCompleteEvent.message.stop_reason == 'error'
        AND there's no partial text, the snapshot lands on ErrorEvent.
        Provider already billed those tokens."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        # One LLM call burned tokens before the upstream failure.
        backend._handle_event(MessageEndEvent(message=_msg(100, 50)))

        # Failure path: PromptTurnCompleteEvent carries an error
        # AssistantMessage with no text.
        err_msg = AssistantMessage(
            model="claude-sonnet-4-6",
            stop_reason="error",
            error_message="upstream timeout",
        )
        backend._handle_event(PromptTurnCompleteEvent(message=err_msg))

        await _drain([])

        error_events = [e for e in listener.events if isinstance(e, ErrorEvent)]
        assert len(error_events) == 1
        snap = error_events[0].usage
        assert snap is not None
        assert snap.input_tokens == 100
        assert snap.output_tokens == 50

    async def test_error_with_partial_text_attaches_snapshot_to_result(self) -> None:
        """When the failure path has partial streamed text, the
        snapshot rides on the ResultEvent (so the user-facing row
        gets the cost) and ErrorEvent.usage stays None to avoid
        double-billing the same tokens to two rows."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        backend._handle_event(MessageEndEvent(message=_msg(100, 50)))

        from pi.ai.types import TextContent

        err_msg = AssistantMessage(
            model="claude-sonnet-4-6",
            stop_reason="error",
            error_message="timeout mid-stream",
        )
        err_msg.content = [TextContent(text="streamed half a paragraph")]
        backend._handle_event(PromptTurnCompleteEvent(message=err_msg))

        await _drain([])

        result_events = [e for e in listener.events if isinstance(e, ResultEvent)]
        error_events = [e for e in listener.events if isinstance(e, ErrorEvent)]
        assert len(result_events) == 1
        assert len(error_events) == 1
        # ResultEvent gets the snapshot — the assistant_message row
        # downstream gets the cost.
        assert result_events[0].usage is not None
        assert result_events[0].usage.input_tokens == 100
        # ErrorEvent.usage stays None — no double-counting.
        assert error_events[0].usage is None


class TestPiAbortedStopReasonResetsAcc:
    async def test_aborted_stop_reason_resets_acc_no_emit(self) -> None:
        """When abort() races with a partial response, the
        AgentSession may yield a PromptTurnCompleteEvent with
        stop_reason='aborted' AFTER abort()'s own StoppedEvent. The
        acc must reset on this branch — otherwise the leftover
        post-abort tokens taint the next prompt."""
        backend = PiBackend()
        listener = _CapturingListener()
        backend.subscribe(listener)

        backend._handle_event(MessageEndEvent(message=_msg(100, 50)))

        aborted = AssistantMessage(
            model="claude-sonnet-4-6",
            stop_reason="aborted",
        )
        backend._handle_event(PromptTurnCompleteEvent(message=aborted))

        # Acc must be reset.
        assert backend._usage_acc.is_empty()
