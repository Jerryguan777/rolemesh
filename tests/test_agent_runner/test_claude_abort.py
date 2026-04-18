"""Regression tests for Claude backend Stop behavior.

Two bugs motivated these tests:

  Symptom A — context pollution via follow-up race:
    User clicks Stop, Q2 arrives via the orchestrator's poll_nats task
    while the SDK is still processing Q1, handle_follow_up pushes Q2 into
    the same MessageStream. MessageStream's __aiter__ drains the queue
    before checking _done, so even though stream.end() was called the SDK
    reads Q2 and produces a combined Q1+Q2 reply.

  Symptom B — late ResultMessage after idle:
    stream.end() only closes the user-input side; the Claude CLI subprocess
    keeps the current LLM request in flight. Its ResultMessage arrives at
    run_prompt's async-for loop after StoppedEvent, gets forwarded to the
    UI, and an assistant bubble appears while the UI is already 'idle'.

Fix:
  - run_prompt wraps the SDK async-for loop in a cancellable Task.
  - abort() cancels that task, sets an _aborting guard, emits StoppedEvent.
  - handle_follow_up rejects pushes while _aborting is True.
  - On cancel, run_prompt rewinds _last_assistant_uuid to the pre-prompt
    snapshot so the next turn's resume-session-at anchor doesn't chain
    through Q1's partial output.

These tests stub `query()` at the module level because claude_agent_sdk
isn't installed outside the agent container. They drive real code paths
on ClaudeBackend — not mock shells — so they catch the same class of
timing bugs the earlier Pi abort-rewind tests missed (sync test +
asyncio.ensure_future trap).
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest


# claude_agent_sdk is only shipped inside the agent container image. Stub it
# in sys.modules BEFORE importing claude_backend so the module-level
# `from claude_agent_sdk import ...` resolves. The individual tests replace
# these stubs with per-test fakes via monkeypatch.
_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
_fake_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)

from agent_runner import claude_backend  # noqa: E402
from agent_runner.backend import BackendEvent, ResultEvent, StoppedEvent  # noqa: E402


# ---------------------------------------------------------------------------
# Fake SDK message types — duck-typed to what claude_backend inspects.
# ---------------------------------------------------------------------------


@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict[str, Any] = field(default_factory=lambda: {"session_id": "sid-fake"})


@dataclass
class AssistantMessage:
    uuid: str = "asst-uuid"
    content: list[Any] = field(default_factory=list)


@dataclass
class ResultMessage:
    result: str | None = "fake reply text"
    session_id: str | None = "sid-fake"


def _fake_query_factory(messages: list[Any], hold_before_result: asyncio.Event | None = None) -> Any:
    """Build a replacement for claude_backend.query that yields `messages`.

    If `hold_before_result` is provided, the factory awaits it before
    yielding the final ResultMessage — lets a test interleave abort()
    with in-flight SDK processing.
    """

    def _query(**kwargs: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            for m in messages:
                if isinstance(m, ResultMessage) and hold_before_result is not None:
                    await hold_before_result.wait()
                yield m

        return _gen()

    return _query


class _RecordingListener:
    def __init__(self) -> None:
        self.events: list[BackendEvent] = []

    async def __call__(self, event: BackendEvent) -> None:
        self.events.append(event)


def _make_backend(monkeypatch: pytest.MonkeyPatch, fake_query: Any) -> tuple[Any, _RecordingListener]:
    """Construct a ClaudeBackend with stubbed SDK pieces."""
    # Patch the SDK entry points on the module (query, ClaudeAgentOptions, HookMatcher).
    monkeypatch.setattr(claude_backend, "query", fake_query, raising=False)

    # ClaudeAgentOptions and HookMatcher are constructed with kwargs but
    # never inspected by the test fakes — stub with permissive dataclasses.
    @dataclass
    class _Opts:
        # Accept any kwargs
        pass

    def _opts_factory(**kwargs: Any) -> _Opts:
        return _Opts()

    def _hook_factory(**kwargs: Any) -> Any:
        return object()

    monkeypatch.setattr(claude_backend, "ClaudeAgentOptions", _opts_factory, raising=False)
    monkeypatch.setattr(claude_backend, "HookMatcher", _hook_factory, raising=False)
    monkeypatch.setattr(claude_backend, "create_rolemesh_mcp_server", lambda ctx: object(), raising=False)

    backend = claude_backend.ClaudeBackend()
    listener = _RecordingListener()
    backend.subscribe(listener)
    return backend, listener


@pytest.fixture
def init_data() -> Any:
    """Minimal AgentInitData-shaped stub for ClaudeBackend.start()."""

    @dataclass
    class _Init:
        session_id: str | None = None
        assistant_name: str | None = "TestBot"
        permissions: dict[str, Any] = field(default_factory=dict)
        system_prompt: str | None = None
        mcp_servers: list[Any] | None = None
        user_id: str | None = None

    return _Init()


# ---------------------------------------------------------------------------
# Symptom B — late ResultMessage must not reach the UI after abort
# ---------------------------------------------------------------------------


async def test_abort_cancels_query_before_result_emitted(
    monkeypatch: pytest.MonkeyPatch,
    init_data: Any,
) -> None:
    """ResultMessage held up behind an event: abort() must cancel the query
    task so the held message never becomes a ResultEvent. The UI must not
    see a late assistant bubble after it has returned to idle."""

    hold = asyncio.Event()
    fake_query = _fake_query_factory(
        messages=[SystemMessage(), AssistantMessage(), ResultMessage()],
        hold_before_result=hold,
    )
    backend, listener = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object())

    # Fire run_prompt on a background task; it'll block inside _consume_query
    # waiting for `hold` before the ResultMessage is yielded.
    prompt_task = asyncio.create_task(backend.run_prompt("Q1"))

    # Let the query_task actually start and consume the first couple of
    # messages (System + Assistant), then hang on `hold`.
    for _ in range(20):
        await asyncio.sleep(0)
        if backend._query_task is not None and not backend._query_task.done():
            break

    # At this point we've seen init + the pre-result assistant message but
    # NOT the ResultEvent. Abort.
    await backend.abort()

    # Release the hold so the fake generator tries to yield ResultMessage —
    # which should be dropped because the task was cancelled.
    hold.set()
    await prompt_task

    # Regression assertions:
    # 1. StoppedEvent was emitted (UI learned about the abort).
    assert any(isinstance(e, StoppedEvent) for e in listener.events)
    # 2. No ResultEvent carrying the fake text was ever emitted. The held
    #    ResultMessage would have produced `text="fake reply text"` — its
    #    absence means cancel reached the SDK before that chunk was processed.
    result_events = [e for e in listener.events if isinstance(e, ResultEvent)]
    assert not any(e.text == "fake reply text" for e in result_events), (
        f"late ResultMessage leaked past abort: {result_events}"
    )


# ---------------------------------------------------------------------------
# Symptom A — follow-up arriving mid-abort must not feed the aborted turn
# ---------------------------------------------------------------------------


async def test_handle_follow_up_ignored_after_abort(
    monkeypatch: pytest.MonkeyPatch,
    init_data: Any,
) -> None:
    """Follow-up messages arriving after abort() has started must not land
    on the MessageStream — even though the stream object still exists
    during the short window before cancel finishes propagating."""

    hold = asyncio.Event()
    fake_query = _fake_query_factory(
        messages=[SystemMessage(), ResultMessage()],
        hold_before_result=hold,
    )
    backend, _listener = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object())

    prompt_task = asyncio.create_task(backend.run_prompt("Q1"))

    # Wait until the query task exists (stream is live).
    for _ in range(50):
        await asyncio.sleep(0)
        if backend._stream is not None and backend._query_task is not None:
            break
    assert backend._stream is not None

    await backend.abort()

    # The MessageStream may not have been reset yet (run_prompt's finally
    # runs after cancel unwinds). Simulate Q2 arriving via the orchestrator
    # poll right in this window.
    stream_at_abort = backend._stream  # may be None post-finally, that's fine
    await backend.handle_follow_up("Q2 hello")

    if stream_at_abort is not None:
        # The pushed Q2 must NOT be in the queue — _aborting guards it out.
        queued_texts = [
            item["message"]["content"] for item in stream_at_abort._queue
        ]
        assert "Q2 hello" not in queued_texts, (
            f"Q2 leaked into the aborted Q1's stream queue: {queued_texts}"
        )

    hold.set()
    await prompt_task


async def test_abort_rewinds_last_assistant_uuid(
    monkeypatch: pytest.MonkeyPatch,
    init_data: Any,
) -> None:
    """Mid-stream the backend tracks _last_assistant_uuid for the next
    run_prompt's resume-session-at anchor. On abort that tracker must
    snap back to its pre-prompt value — otherwise the next turn's LLM
    context resumes from the aborted Q1's partial output."""

    hold = asyncio.Event()
    # The AssistantMessage sets _last_assistant_uuid mid-stream to
    # "asst-mid". Without rewind it'd stay there after abort.
    fake_query = _fake_query_factory(
        messages=[
            SystemMessage(),
            AssistantMessage(uuid="asst-mid"),
            ResultMessage(),
        ],
        hold_before_result=hold,
    )
    backend, _listener = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object())

    # Simulate a prior completed turn: backend already has a known-good
    # resume anchor we must return to.
    backend._last_assistant_uuid = "asst-Q0-complete"

    prompt_task = asyncio.create_task(backend.run_prompt("Q1"))

    # Wait until the AssistantMessage was consumed (uuid bumped to "asst-mid").
    for _ in range(50):
        await asyncio.sleep(0)
        if backend._last_assistant_uuid == "asst-mid":
            break
    assert backend._last_assistant_uuid == "asst-mid", (
        "test setup error: assistant uuid wasn't observed mid-stream"
    )

    await backend.abort()
    hold.set()
    await prompt_task

    # The rewind puts us back at the pre-prompt anchor.
    assert backend._last_assistant_uuid == "asst-Q0-complete", (
        f"resume anchor not rewound after abort: got {backend._last_assistant_uuid!r}"
    )


async def test_abort_before_query_task_exists_still_suppresses_emits(
    monkeypatch: pytest.MonkeyPatch,
    init_data: Any,
) -> None:
    """Pre-task race: abort() can arrive between run_prompt's first await
    (RunningEvent) and asyncio.create_task(_consume_query()), so _query_task
    is None and cancel() can't land. _aborting is set though, and the
    in-loop guard at the top of _consume_query must short-circuit instead
    of letting the SDK stream emit events for an already-aborted turn.

    Simulates the window by manually setting _aborting=True before calling
    run_prompt, so the consume_query task starts with the flag already set."""

    # Messages that WOULD be emitted if the guard didn't kick in.
    fake_query = _fake_query_factory(
        messages=[SystemMessage(), ResultMessage(result="should-never-appear")],
    )
    backend, listener = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object())

    # Manually recreate the pre-task race state: abort latched True right
    # before run_prompt creates its consumer task.
    backend._aborting = True

    # Run run_prompt — _consume_query should immediately return without
    # draining the SDK generator.
    await backend.run_prompt("Q1")

    # No ResultEvent carrying the fake text should ever have reached the
    # listener — the guard prevented iteration over the SDK generator.
    result_events = [e for e in listener.events if isinstance(e, ResultEvent)]
    assert not any(e.text == "should-never-appear" for e in result_events), (
        f"pre-task abort guard didn't suppress emits: {result_events}"
    )


async def test_abort_without_active_query_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    init_data: Any,
) -> None:
    """abort() called between turns (no query_task) must still emit
    StoppedEvent, not blow up, and crucially NOT leave _aborting latched
    True. Latent True would gag every handle_follow_up that arrives later
    in the same session — the fix must prove this can't happen."""

    fake_query = _fake_query_factory(messages=[])
    backend, listener = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object())

    # No run_prompt in flight when abort fires.
    await backend.abort()

    assert any(isinstance(e, StoppedEvent) for e in listener.events)
    # Regression: previously _aborting stayed True until the next run_prompt
    # because only that path's finally cleared it. abort() now resets
    # _aborting itself when there was no active query, so follow-ups on the
    # NEXT turn work immediately.
    assert backend._aborting is False


async def test_abort_without_active_query_does_not_gag_next_turn_follow_up(
    monkeypatch: pytest.MonkeyPatch,
    init_data: Any,
) -> None:
    """Concrete behavioral test for the latched-flag regression: after an
    idle-state abort(), the NEXT turn's follow-up must land on the stream."""

    hold = asyncio.Event()
    fake_query = _fake_query_factory(
        messages=[SystemMessage(), ResultMessage()],
        hold_before_result=hold,
    )
    backend, _listener = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object())

    # 1. Spurious idle-state abort (e.g. UI misfire).
    await backend.abort()

    # 2. User sends a real message that becomes the next turn.
    prompt_task = asyncio.create_task(backend.run_prompt("Q1"))
    for _ in range(50):
        await asyncio.sleep(0)
        if backend._stream is not None:
            break
    assert backend._stream is not None

    # 3. User sends a follow-up during Q1. Must actually land on the stream.
    await backend.handle_follow_up("Q2 follow-up")
    stream = backend._stream
    assert stream is not None
    queued = [item["message"]["content"] for item in stream._queue]
    assert "Q2 follow-up" in queued, (
        f"follow-up gagged by leaked _aborting latch: {queued}"
    )

    hold.set()
    await prompt_task
