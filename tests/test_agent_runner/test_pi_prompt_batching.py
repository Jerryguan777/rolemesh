"""Regression tests for the Pi follow-up reply-loss bug.

Scenario the tests pin down:
  - run_prompt("msg1") starts; Pi's AgentSession.prompt() is blocking
  - A follow-up ("msg2") is queued mid-flight via follow_up()
  - Pi's agent_loop answers msg1, then re-enters with msg2 queued, then answers msg2
  - The outer run_prompt() returns once, after BOTH replies

The historical bug: PiBackend tracked `_last_result_text` on every TurnEndEvent
and emitted ResultEvent once at run_prompt end. msg2's reply overwrote msg1's,
so msg1 vanished from the NATS results stream.

The fix: agent_loop emits PromptTurnCompleteEvent once per answered user
message; PiBackend translates each into a separate ResultEvent(is_final=False).

These tests intentionally do NOT mock AgentSession or agent_loop. They drive
agent_loop directly with a scripted stream_fn so a bug in the real loop (e.g.
a refactor that drops the new event, or emits it in the wrong place) is
caught — not a re-implementation of the loop's shape.
"""

from __future__ import annotations

from typing import Any

from pi.agent.agent_loop import agent_loop
from pi.agent.types import (
    AgentContext,
    AgentEndEvent,
    AgentLoopConfig,
    PromptTurnCompleteEvent,
    TurnEndEvent,
)
from pi.ai.types import (
    AssistantMessage,
    DoneEvent,
    Model,
    TextContent,
    UserMessage,
)


def _model() -> Model:
    return Model(id="test-model", name="test", api="openai-completions", provider="test")


def _text_of(msg: Any) -> str:
    content = getattr(msg, "content", [])
    if isinstance(content, str):
        return content
    return "".join(b.text for b in content if isinstance(b, TextContent))


def _make_scripted_stream_fn(replies: list[str]) -> Any:
    """Return a stream_fn that emits one DoneEvent per invocation, pulling the
    reply text from `replies` in order. No tool calls."""

    counter = {"i": 0}

    async def stream_fn(model: Model, context: Any, opts: Any) -> Any:
        i = counter["i"]
        counter["i"] = i + 1
        assert i < len(replies), (
            f"stream_fn called {i + 1} times but only {len(replies)} replies scripted"
        )
        message = AssistantMessage(
            api=model.api,
            provider=model.provider,
            model=model.id,
            content=[TextContent(text=replies[i])],
            stop_reason="stop",
        )
        yield DoneEvent(reason="stop", message=message)

    return stream_fn


async def test_single_prompt_emits_one_prompt_turn_complete() -> None:
    """Sanity: a single prompt, no follow-ups, yields exactly one
    PromptTurnCompleteEvent whose message carries the assistant reply."""

    prompts = [UserMessage(content=[TextContent(text="hello")])]
    context = AgentContext(system_prompt="", messages=[], tools=[])
    config = AgentLoopConfig(model=_model(), max_turns=5)

    events = [
        e
        async for e in agent_loop(
            prompts, context, config, stream_fn=_make_scripted_stream_fn(["hi there"])
        )
    ]

    completes = [e for e in events if isinstance(e, PromptTurnCompleteEvent)]
    assert len(completes) == 1
    assert _text_of(completes[0].message) == "hi there"

    # Must come before AgentEndEvent — consumers rely on this ordering to know
    # "this reply is final for the current prompt" strictly before "all prompts
    # are done".
    assert isinstance(events[-1], AgentEndEvent)
    last_complete_idx = next(
        i for i, e in enumerate(events) if isinstance(e, PromptTurnCompleteEvent)
    )
    assert last_complete_idx < len(events) - 1


async def test_queued_follow_up_produces_two_prompt_turn_completes() -> None:
    """Core regression: if a follow-up is queued during msg1's processing,
    the loop must emit two PromptTurnCompleteEvents with the two distinct
    replies. Before the fix, msg1's final assistant message was discoverable
    only in a TurnEndEvent, and PiBackend's _last_result_text got overwritten
    by msg2's reply before ResultEvent was emitted."""

    prompts = [UserMessage(content=[TextContent(text="msg1")])]
    follow_up_msg = UserMessage(content=[TextContent(text="msg2")])

    context = AgentContext(system_prompt="", messages=[], tools=[])

    # Simulate a follow-up queued between msg1's answer and the outer loop's
    # follow-up poll. The agent_loop calls get_follow_up_messages after an
    # inner loop settles; returning [msg2] on the first call re-enters the
    # inner loop for msg2.
    follow_up_calls = {"n": 0}

    def get_follow_up() -> list[Any]:
        follow_up_calls["n"] += 1
        if follow_up_calls["n"] == 1:
            return [follow_up_msg]
        return []

    config = AgentLoopConfig(
        model=_model(),
        max_turns=10,
        get_follow_up_messages=get_follow_up,
    )

    events = [
        e
        async for e in agent_loop(
            prompts,
            context,
            config,
            stream_fn=_make_scripted_stream_fn(["reply to msg1", "reply to msg2"]),
        )
    ]

    completes = [e for e in events if isinstance(e, PromptTurnCompleteEvent)]
    assert len(completes) == 2, (
        f"expected 2 per-prompt completions (one per user message); got {len(completes)}: "
        f"{[_text_of(e.message) for e in completes]}"
    )

    # The assertion that catches the original bug: msg1's reply is NOT lost,
    # and msg2's reply is distinct.
    assert _text_of(completes[0].message) == "reply to msg1"
    assert _text_of(completes[1].message) == "reply to msg2"

    # get_follow_up_messages is polled: once after msg1's inner loop exits
    # (returns [msg2], triggering re-entry), and once after msg2 exits
    # (returns [], breaking the outer loop).
    assert follow_up_calls["n"] == 2


async def test_follow_up_poll_order_each_complete_precedes_its_inner_loop_exit() -> None:
    """Ordering invariant: for each user message, the sequence is
    TurnEndEvent+ ... → PromptTurnCompleteEvent → (optional) next MessageStartEvent.

    A consumer that translates PromptTurnCompleteEvent into an outbound
    ResultEvent relies on this: the event must come AFTER the assistant's
    final TurnEndEvent for that prompt, not before."""

    prompts = [UserMessage(content=[TextContent(text="msg1")])]
    follow_up_msg = UserMessage(content=[TextContent(text="msg2")])
    context = AgentContext(system_prompt="", messages=[], tools=[])

    calls = {"n": 0}

    def get_follow_up() -> list[Any]:
        calls["n"] += 1
        return [follow_up_msg] if calls["n"] == 1 else []

    config = AgentLoopConfig(
        model=_model(), max_turns=10, get_follow_up_messages=get_follow_up,
    )

    events = [
        e
        async for e in agent_loop(
            prompts,
            context,
            config,
            stream_fn=_make_scripted_stream_fn(["r1", "r2"]),
        )
    ]

    # Find PromptTurnCompleteEvent indices and verify each has a preceding
    # TurnEndEvent that isn't separated by another PromptTurnCompleteEvent.
    complete_idxs = [i for i, e in enumerate(events) if isinstance(e, PromptTurnCompleteEvent)]
    assert len(complete_idxs) == 2

    for i, idx in enumerate(complete_idxs):
        prefix = events[: idx]
        # The most recent preceding TurnEndEvent must carry the same message
        # as the completion marker (the final assistant turn for this prompt).
        last_turn_end = next(
            (e for e in reversed(prefix) if isinstance(e, TurnEndEvent)),
            None,
        )
        assert last_turn_end is not None, f"completion #{i} has no preceding TurnEndEvent"
        assert _text_of(last_turn_end.message) == _text_of(events[idx].message)


async def test_max_turns_still_emits_completion_marker() -> None:
    """When the inner loop hits max_turns, it still marks the prompt as
    complete so the bridge emits a final ResultEvent — otherwise run_prompt
    would return without ever publishing a reply for this user message."""

    prompts = [UserMessage(content=[TextContent(text="loopy")])]
    context = AgentContext(system_prompt="", messages=[], tools=[])
    config = AgentLoopConfig(model=_model(), max_turns=0)  # hits limit immediately

    events = [
        e async for e in agent_loop(prompts, context, config, stream_fn=_make_scripted_stream_fn([]))
    ]

    completes = [e for e in events if isinstance(e, PromptTurnCompleteEvent)]
    assert len(completes) == 1
    # The completion marker carries the error-stopped assistant message.
    msg = completes[0].message
    assert isinstance(msg, AssistantMessage)
    assert msg.stop_reason == "error"
