"""Regression tests for context pollution after Stop.

Scenario (reported, end-to-end reproducible):
  - User asks Q1 (a substantive question).
  - Pi streams partial assistant reply, user clicks Stop mid-stream.
  - Abort fires; a SessionMessageEntry for Q1 + an aborted assistant entry
    are already on disk and chained as the active leaf.
  - User asks Q2 (unrelated, e.g. "hello" or "who are you").
  - Q2 was persisted as a child of the aborted assistant, so
    build_session_context() walks Q2 -> aborted_assistant -> Q1 -> ... and
    Pi's next LLM call sees the cancelled question again. Reply conflates
    both questions ("Hello Jerry. Here are the MCP tools I have access to…").

Fix: rewind session_manager._leaf_id to a snapshot taken at the start of
the aborted prompt, AND rebuild agent._state.messages from that rewound
leaf. The aborted turn stays on disk as an orphan branch.

What the earlier SessionManager-only tests failed to catch:
  The real runtime path flows through AgentSession.abort(), which awaits
  wait_for_idle() — during that await the agent_end event handler runs
  and clears the field abort() was originally inspecting. The original
  fix guarded the rewind on `self._last_assistant_message is not None`,
  which is *always* None by the time wait_for_idle returns, so the
  rewind branch silently never executed. A test that only manipulated
  SessionManager state bypassed the AgentSession event pipeline and
  missed the timing bug entirely.

This file now tests the mechanism at two levels:
  1. SessionManager rewind semantics (leaf manipulation + context walk).
  2. AgentSession.abort() timing — event dispatch order does not clear
     the signal abort() uses to decide whether to rewind.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from pi.agent.agent import Agent, AgentOptions
from pi.agent.types import AgentEndEvent, MessageEndEvent
from pi.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    Model,
    SimpleStreamOptions,
    TextContent,
    UserMessage,
)
from pi.coding_agent.core.agent_session import AgentSession, AgentSessionConfig
from pi.coding_agent.core.session_manager import SessionManager


def _assistant(text: str, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-responses",
        provider="openai",
        model="gpt-4o-mini",
        stop_reason=stop_reason,  # type: ignore[arg-type]
    )


def _user(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def _texts(messages: list) -> list[str]:
    """Extract text content per message for readable assertions."""
    out: list[str] = []
    for m in messages:
        content = getattr(m, "content", "")
        if isinstance(content, str):
            out.append(content)
        else:
            parts = [b.text for b in content if isinstance(b, TextContent)]
            out.append("".join(parts))
    return out


@pytest.fixture
def sm(tmp_path: Path) -> SessionManager:
    """Fresh SessionManager writing to a tmp file."""
    mgr = SessionManager.create(str(tmp_path))
    mgr.set_session_file(str(tmp_path / "session.jsonl"))
    return mgr


def test_context_excludes_aborted_branch_after_leaf_rewind(sm: SessionManager) -> None:
    """Core bug repro: without rewind, Q2's context includes Q1+aborted.
    After rewind to pre-Q1 leaf, context for Q2 contains only Q0 turn
    and Q2 itself."""

    # Q0 turn (completed)
    sm.append_message(_user("Q0 what is 2+2"))
    sm.append_message(_assistant("Q0 reply: 4"))

    # Snapshot leaf BEFORE Q1 — this is what AgentSession.prompt() captures.
    pre_q1_leaf = sm.get_leaf_id()

    # Q1 turn, user cancels mid-stream: partial aborted assistant appended
    sm.append_message(_user("Q1 pls show me 7Cap_T1_US by country exposure"))
    sm.append_message(_assistant("Q1 partial: I can help, which do you mean by country...", stop_reason="aborted"))

    # Sanity: without rewind the active chain would carry Q1 + aborted
    # forward to the next turn. Verify that first.
    sm.append_message(_user("Q2 who are you"))
    ctx_without_rewind = sm.build_session_context()
    texts_no_rewind = _texts(ctx_without_rewind.messages)
    assert any("Q1" in t for t in texts_no_rewind), (
        "sanity check failed: without rewind, Q1 should still appear in the chain"
    )

    # Now simulate what abort() does:
    #   1. Remove Q2 we just appended (it was appended on top of the dirty chain)
    #   2. Rewind to pre-Q1 leaf
    #   3. Re-append Q2 (same text) — this is what the next prompt() would do
    # Step 1 is only needed in this test because we already appended Q2 above
    # for the sanity check; in real flow Q2 is appended after rewind.
    sm._leaf_id = pre_q1_leaf
    sm.append_message(_user("Q2 who are you"))

    ctx = sm.build_session_context()
    texts = _texts(ctx.messages)

    # Active chain after rewind: Q0_user, Q0_reply, Q2. No Q1.
    assert any("Q0 what is 2+2" in t for t in texts)
    assert any("Q0 reply: 4" in t for t in texts)
    assert any("Q2 who are you" in t for t in texts)
    # The regression assertion — this is the line that catches the original bug.
    assert not any("Q1" in t for t in texts), (
        f"aborted turn leaked into Q2's context: {texts}"
    )
    assert not any("partial" in t for t in texts), (
        f"aborted partial reply leaked into Q2's context: {texts}"
    )


def test_rewind_to_none_handles_empty_pre_prompt_state(sm: SessionManager) -> None:
    """When the user aborts their very first message (no prior turns), the
    pre-prompt leaf snapshot is None. reset_leaf() must produce a clean
    context for the next turn."""

    # Pre-prompt state: empty session, leaf is None
    assert sm.get_leaf_id() is None

    # User sends Q1 and aborts immediately
    sm.append_message(_user("Q1 first ever message"))
    sm.append_message(_assistant("aborted partial", stop_reason="aborted"))

    # Rewind: equivalent to reset_leaf() since pre-prompt leaf was None
    sm.reset_leaf()
    assert sm.get_leaf_id() is None

    # Q2 lands at the root
    sm.append_message(_user("Q2 second message"))

    ctx = sm.build_session_context()
    texts = _texts(ctx.messages)
    assert texts == ["Q2 second message"], (
        f"expected only Q2, got {texts}"
    )


# ---------------------------------------------------------------------------
# AgentSession-level tests — exercise the abort() event-dispatch timing that
# the SessionManager-only tests above bypass. These are the tests that would
# have caught the original "rewind branch never runs" bug.
# ---------------------------------------------------------------------------


def _aborted_assistant() -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api="openai-responses",
        provider="openai",
        model="gpt-4o-mini",
        stop_reason="aborted",
    )


def _make_agent_session(tmp_path: Path, stream_fn: Any) -> tuple[AgentSession, SessionManager]:
    """Build a minimally wired AgentSession for abort-path testing."""
    sm = SessionManager.create(str(tmp_path))
    sm.set_session_file(str(tmp_path / "session.jsonl"))

    agent = Agent(AgentOptions(
        stream_fn=stream_fn,
        max_turns=3,
    ))
    # Pin a real model so stream_opts validation in agent_loop passes.
    agent._state.model = Model(
        id="gpt-4o-mini",
        name="test",
        api="openai-responses",
        provider="openai",
    )

    cfg = AgentSessionConfig(agent=agent, session_manager=sm, cwd=str(tmp_path))
    session = AgentSession(cfg)
    return session, sm


async def test_event_dispatch_timing_preserves_abort_signal() -> None:
    """Regression for the *silent* bug in the previous fix: agent_end fires
    inside wait_for_idle(), clearing _last_assistant_message before abort()
    gets to inspect it. This test simulates the exact dispatch sequence and
    pins that the signal abort() relies on (_last_turn_aborted) survives.

    Declared async because _handle_agent_event's agent_end branch calls
    asyncio.ensure_future for the compaction task — that needs a running
    event loop. In Python 3.14+ running this synchronously would raise
    RuntimeError instead of the 3.12/3.13 DeprecationWarning."""

    # Don't need a real session_manager file — we're only poking the event
    # handler's flag logic. Use a stub that records append_message calls.
    class _StubSM:
        def __init__(self) -> None:
            self.appends: list[Any] = []

        def append_message(self, msg: Any) -> str:
            self.appends.append(msg)
            return "id"

        def append_custom_message_entry(self, *a: Any, **kw: Any) -> str:
            return "id"

    # Stub for the compaction hook so the agent_end branch's background
    # asyncio.ensure_future doesn't crash with AttributeError on missing
    # _settings_manager. Returning enabled=False lets _check_compaction
    # bail out immediately.
    class _StubSettingsManager:
        def get_compaction_settings(self) -> dict[str, Any]:
            return {"enabled": False}

    # Minimal AgentSession just to call _handle_agent_event on.
    session = AgentSession.__new__(AgentSession)
    session._session_manager = _StubSM()  # type: ignore[attr-defined]
    session._settings_manager = _StubSettingsManager()  # type: ignore[attr-defined]
    session._steering_messages = []  # type: ignore[attr-defined]
    session._follow_up_messages = []  # type: ignore[attr-defined]
    session._event_listeners = []  # type: ignore[attr-defined]
    session._last_assistant_message = None  # type: ignore[attr-defined]
    session._last_turn_aborted = False  # type: ignore[attr-defined]
    session._compaction_task = None  # type: ignore[attr-defined]

    # Dispatch in the order the real runtime produces:
    #   message_end (aborted assistant) → agent_end
    session._handle_agent_event(MessageEndEvent(message=_aborted_assistant()))
    # At this point the original code's check would still see the field set
    # via _last_assistant_message. But agent_end clears it:
    session._handle_agent_event(AgentEndEvent())

    # _last_assistant_message was cleared by agent_end — this is what made
    # the previous fix silently no-op. Demonstrate that explicitly:
    assert session._last_assistant_message is None  # type: ignore[attr-defined]

    # The new flag must persist through agent_end so abort() can still act.
    assert session._last_turn_aborted is True, (  # type: ignore[attr-defined]
        "the abort signal used by abort() was cleared by agent_end — rewind will silently skip"
    )


async def test_abort_during_real_prompt_rewinds_session(tmp_path: Path) -> None:
    """End-to-end: feed AgentSession a real Agent with a scripted stream_fn
    that pauses mid-turn until abort fires, then call session.abort() and
    assert session_manager's leaf rewound + agent's in-memory messages
    were rebuilt (no Q1/aborted residue)."""

    stream_entered = asyncio.Event()

    async def scripted_stream_fn(
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AsyncGenerator[Any, None]:
        # Signal we're inside the stream, then block until the agent's
        # real abort_event gets set by session.abort() → agent.abort().
        # This is exactly how Pi's real providers (openai_responses after
        # our mid-stream signal check) abort.
        stream_entered.set()
        assert options is not None and options.signal is not None, (
            "agent_loop should always propagate a non-None abort signal"
        )
        await options.signal.wait()
        raise RuntimeError("Request was aborted")
        # Unreachable, but satisfies the AsyncGenerator contract.
        yield DoneEvent(  # pragma: no cover
            reason="stop",
            message=AssistantMessage(api=model.api, provider=model.provider, model=model.id),
        )

    session, sm = _make_agent_session(tmp_path, scripted_stream_fn)

    # Pre-seed a completed Q0 turn so there's something to rewind *to*.
    sm.append_message(UserMessage(content=[TextContent(text="Q0")]))
    sm.append_message(AssistantMessage(
        content=[TextContent(text="Q0 reply")],
        api="openai-responses",
        provider="openai",
        model="gpt-4o-mini",
        stop_reason="stop",
    ))
    pre_q1_leaf = sm.get_leaf_id()
    assert pre_q1_leaf is not None

    # Also sync agent._state.messages with the session so build/replace is
    # meaningful. (Pi does this in sdk.py:270 during resume.)
    ctx = sm.build_session_context()
    session._agent.replace_messages(ctx.messages)

    # Fire the Q1 prompt on a background task; it blocks inside stream_fn
    # on options.signal.wait() until abort is called.
    async def _run_q1() -> None:
        try:
            await session.prompt("Q1 gets aborted")
        except Exception:
            pass  # abort raises inside the stream; swallow here

    prompt_task = asyncio.create_task(_run_q1())

    # Wait until stream_fn actually parked on the signal, then abort.
    await asyncio.wait_for(stream_entered.wait(), timeout=2.0)
    assert session.is_streaming

    await session.abort()
    await prompt_task

    # Regression assertions:
    # 1. session_manager's active leaf rewound to the pre-Q1 snapshot.
    assert sm.get_leaf_id() == pre_q1_leaf, (
        f"leaf not rewound: expected {pre_q1_leaf!r}, got {sm.get_leaf_id()!r}. "
        "abort() likely skipped the rewind branch."
    )

    # 2. Agent's in-memory message list no longer contains the aborted
    #    turn's user/assistant entries.
    def _texts(msgs: list[Any]) -> list[str]:
        out: list[str] = []
        for m in msgs:
            content = getattr(m, "content", "")
            if isinstance(content, str):
                out.append(content)
            else:
                parts = [b.text for b in content if isinstance(b, TextContent)]
                out.append("".join(parts))
        return out

    agent_msg_texts = _texts(session._agent._state.messages)
    assert not any("aborted" in t for t in agent_msg_texts), (
        f"aborted-turn content leaked into agent memory: {agent_msg_texts}"
    )
    assert not any("Q1" in t for t in agent_msg_texts), (
        f"Q1 leaked into agent memory after rewind: {agent_msg_texts}"
    )
    assert any("Q0" in t for t in agent_msg_texts), (
        f"expected Q0 to remain in agent memory: {agent_msg_texts}"
    )


async def test_abort_clears_follow_up_queue_so_no_phantom_on_next_turn(tmp_path: Path) -> None:
    """After abort, any Q2 that was queued mid-flight via follow_up() must
    NOT resurface on the next turn. Previously the rewind cleared the
    session tree but left agent._follow_up_queue populated; _run_loop's
    outer get_follow_up_messages() poll would pull the ghost Q2 out on a
    subsequent turn and process it as a phantom continuation."""

    from pi.ai.types import UserMessage

    sm = SessionManager.create(str(tmp_path))
    sm.set_session_file(str(tmp_path / "session.jsonl"))

    async def scripted_stream_fn(model: Model, context: Any, options: Any) -> Any:
        # Block forever until aborted — gives us a window to queue Q2.
        if options is not None and options.signal is not None:
            await options.signal.wait()
            raise RuntimeError("Request was aborted")
        yield DoneEvent(  # pragma: no cover
            reason="stop",
            message=AssistantMessage(api=model.api, provider=model.provider, model=model.id),
        )

    agent = Agent(AgentOptions(stream_fn=scripted_stream_fn, max_turns=3))
    agent._state.model = Model(id="gpt-4o-mini", name="test", api="openai-responses", provider="openai")

    cfg = AgentSessionConfig(agent=agent, session_manager=sm, cwd=str(tmp_path))
    session = AgentSession(cfg)

    # Start Q1, queue Q2 as a follow-up while Q1 is in flight.
    prompt_task = asyncio.create_task(session.prompt("Q1"))

    for _ in range(50):
        await asyncio.sleep(0)
        if session.is_streaming:
            break
    assert session.is_streaming

    # Queue Q2 as a follow-up (lands in agent._follow_up_queue).
    agent.follow_up(UserMessage(content=[TextContent(text="Q2 ghost")]))
    assert agent.has_queued_messages(), "Q2 should be queued before abort"

    # Abort — the rewind must also clear the follow-up queue.
    await session.abort()
    await prompt_task

    assert not agent.has_queued_messages(), (
        "Q2 survived abort on agent._follow_up_queue — will resurface as "
        "a phantom on the next turn."
    )


def test_orphaned_branch_remains_in_file_entries(sm: SessionManager) -> None:
    """Aborted entries should stay in the linear file for debug/audit, only
    excluded from the parent-chain walk. Regression guards against a future
    'fix' that deletes entries outright."""

    sm.append_message(_user("Q0"))
    sm.append_message(_assistant("Q0 reply"))
    pre = sm.get_leaf_id()

    sm.append_message(_user("Q1 to be orphaned"))
    sm.append_message(_assistant("Q1 partial", stop_reason="aborted"))

    sm._leaf_id = pre
    sm.append_message(_user("Q2"))

    # build_session_context (active chain walk): Q1 absent
    texts_active = _texts(sm.build_session_context().messages)
    assert not any("orphaned" in t for t in texts_active)

    # But get_entries (flat file) still has all 5 messages
    all_message_texts = _texts(
        [getattr(e, "message", None) for e in sm.get_entries() if hasattr(e, "message")]
    )
    assert any("Q1 to be orphaned" in t for t in all_message_texts), (
        "orphaned entry was removed from the file — should have stayed for audit"
    )
    assert any("Q1 partial" in t for t in all_message_texts)
