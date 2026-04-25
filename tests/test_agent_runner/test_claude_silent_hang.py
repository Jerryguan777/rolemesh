"""Claude backend: silent SDK termination must surface as ErrorEvent.

The Claude SDK can finish its async-for stream without raising AND
without yielding any ResultMessage when an upstream HTTP failure
(egress 403, rate-limit, timeout) is swallowed inside the SDK rather
than surfaced as an exception. Pre-fix, ``run_prompt`` returned with
``error_raised=False``, ``_emit_stop("completed")`` was sent, no
ResultEvent was ever published, and the orchestrator believed the turn
succeeded — Telegram user saw nothing.

This is the Claude-side counterpart of ``pi_backend._handle_event``'s
error translation. Pi can identify the upstream failure precisely from
``PromptTurnCompleteEvent.stop_reason="error"``; Claude SDK gives us no
comparable signal, so the fix is a coarser terminal invariant in
``run_prompt`` finally:

  Stream ended cleanly + no ResultMessage + not aborted + no exception
    ⇒ synthesize ErrorEvent + flip Stop reason to "error".

Tests pin five corners:

  1. SDK stream ending without ResultMessage → ErrorEvent emitted with
     a message that makes the upstream-error hypothesis legible to the
     user, AND ``Stop("error")`` is sent (not the misleading "completed").
  2. SDK stream ending immediately (zero messages) → same treatment as
     case 1; the empty-stream variant must not bypass the guard.
  3. Healthy turn (>=1 ResultMessage) → no synthetic ErrorEvent (regression
     guard so the fix doesn't break the common path).
  4. Aborted path (cancel before any ResultMessage) → no synthetic
     ErrorEvent. abort() owns the StoppedEvent on this path.
  5. SDK raising an exception → existing except-Exception path still
     emits its precise ErrorEvent; the finally guard does NOT pile on
     a second ErrorEvent for the same failure.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator  # noqa: TC003 — used at runtime in async generator annotations
from dataclasses import dataclass, field
from typing import Any

import pytest

# claude_agent_sdk is only shipped inside the agent container image. Stub
# it before importing claude_backend so the module-level imports resolve.
_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
_fake_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: None})  # type: ignore[attr-defined]
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)

from agent_runner import claude_backend  # noqa: E402
from agent_runner.backend import (  # noqa: E402
    BackendEvent,
    ErrorEvent,
    ResultEvent,
    StoppedEvent,
)
from agent_runner.hooks import HookRegistry, StopEvent  # noqa: E402

# ---------------------------------------------------------------------------
# Fake SDK message types
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


def _query_yielding(messages: list[Any]) -> Any:
    """Build a stub SDK query that yields the given messages and returns."""

    def _query(**kwargs: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            for m in messages:
                yield m

        return _gen()

    return _query


def _query_raising(exc: BaseException) -> Any:
    """Build a stub SDK query that raises after yielding (or before)."""

    def _query(**kwargs: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            yield SystemMessage()
            raise exc

        return _gen()

    return _query


class _RecordingListener:
    def __init__(self) -> None:
        self.events: list[BackendEvent] = []

    async def __call__(self, event: BackendEvent) -> None:
        self.events.append(event)


class _RecordingStopHandler:
    """Capture StopEvent.reason values via the hook bus.

    Stop reason has no consumer in the repo today, but the silent-hang fix
    flips reason from "completed" → "error" on the synthetic-ErrorEvent
    path. Pinning the reason here protects future observability hooks from
    an asymmetry in the Stop signal.
    """

    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def on_stop(self, event: StopEvent) -> None:
        self.reasons.append(event.reason)


@pytest.fixture
def init_data() -> Any:
    @dataclass
    class _Init:
        session_id: str | None = None
        assistant_name: str | None = "TestBot"
        permissions: dict[str, Any] = field(default_factory=dict)
        system_prompt: str | None = None
        mcp_servers: list[Any] | None = None
        user_id: str | None = None
        is_scheduled_task: bool = False

    return _Init()


def _make_backend(
    monkeypatch: pytest.MonkeyPatch, fake_query: Any
) -> tuple[Any, _RecordingListener, _RecordingStopHandler]:
    """Build a ClaudeBackend with stubbed SDK pieces and a stop-reason recorder."""
    monkeypatch.setattr(claude_backend, "query", fake_query, raising=False)

    @dataclass
    class _Opts:
        pass

    monkeypatch.setattr(claude_backend, "ClaudeAgentOptions", lambda **k: _Opts(), raising=False)
    monkeypatch.setattr(claude_backend, "HookMatcher", lambda **k: object(), raising=False)
    monkeypatch.setattr(
        claude_backend,
        "create_rolemesh_mcp_server",
        lambda ctx, **kwargs: object(),
        raising=False,
    )

    backend = claude_backend.ClaudeBackend()
    listener = _RecordingListener()
    backend.subscribe(listener)
    stop_recorder = _RecordingStopHandler()
    return backend, listener, stop_recorder


def _stop_hooks(stop_recorder: _RecordingStopHandler) -> HookRegistry:
    reg = HookRegistry()
    reg.register(stop_recorder)
    return reg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_stream_with_no_result_message_synthesizes_error(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """The actual bug: SDK yields a SystemMessage(init) and an
    AssistantMessage but never a ResultMessage, then ends cleanly. Without
    the guard, run_prompt would return having published zero ResultEvents
    and the user would see silence."""
    fake_query = _query_yielding(
        [SystemMessage(), AssistantMessage()]  # NB: no ResultMessage
    )
    backend, listener, stop_recorder = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object(), hooks=_stop_hooks(stop_recorder))

    await backend.run_prompt("hello")

    error_events = [e for e in listener.events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    msg = error_events[0].error
    # Error message must surface the upstream-error hypothesis so the user
    # / operator can act on it. Loose match — exact wording can drift.
    assert "ResultMessage" in msg or "upstream" in msg.lower()

    # No ResultEvent must be present — silent path produced no real reply.
    assert not any(isinstance(e, ResultEvent) for e in listener.events)

    # Stop reason flipped from misleading "completed" to "error".
    assert stop_recorder.reasons == ["error"]


async def test_empty_stream_synthesizes_error(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """Edge: SDK ends immediately, zero messages. Same fault class as
    above — the guard must not be conditioned on any minimum message count."""
    fake_query = _query_yielding([])
    backend, listener, stop_recorder = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object(), hooks=_stop_hooks(stop_recorder))

    await backend.run_prompt("hello")

    error_events = [e for e in listener.events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert stop_recorder.reasons == ["error"]


async def test_healthy_turn_with_result_does_not_synthesize_error(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """Regression guard: a normal turn with at least one ResultMessage must
    NOT receive a synthetic ErrorEvent. Without this, the silent-hang fix
    would false-positive every successful run."""
    fake_query = _query_yielding(
        [SystemMessage(), AssistantMessage(), ResultMessage()]
    )
    backend, listener, stop_recorder = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object(), hooks=_stop_hooks(stop_recorder))

    await backend.run_prompt("hello")

    error_events = [e for e in listener.events if isinstance(e, ErrorEvent)]
    assert error_events == []
    result_events = [e for e in listener.events if isinstance(e, ResultEvent)]
    assert len(result_events) == 1
    assert result_events[0].text == "fake reply text"
    assert stop_recorder.reasons == ["completed"]


async def test_aborted_path_does_not_synthesize_error(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """abort() races with run_prompt; the cancellation may land before any
    ResultMessage is yielded. result_count==0 in this case is legitimate
    (user cancelled, no upstream failure) — synthesizing an ErrorEvent here
    would lie to the user.

    Drives a real cancel: build a query that hangs forever, kick run_prompt,
    fire abort() while the consume task is mid-stream.
    """
    hang_forever = asyncio.Event()  # never set

    def _query(**kwargs: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            yield SystemMessage()
            await hang_forever.wait()
            yield ResultMessage()  # never reached

        return _gen()

    backend, listener, stop_recorder = _make_backend(monkeypatch, _query)
    await backend.start(init_data, tool_ctx=object(), hooks=_stop_hooks(stop_recorder))

    prompt_task = asyncio.create_task(backend.run_prompt("hello"))

    # Yield enough times for the consume task to start and yield SystemMessage,
    # then enter the wait on hang_forever.
    for _ in range(40):
        await asyncio.sleep(0)
        if backend._query_task is not None and not backend._query_task.done():
            break

    await backend.abort()
    await prompt_task

    # No synthetic ErrorEvent on the abort path.
    synth_errors = [
        e for e in listener.events
        if isinstance(e, ErrorEvent)
        and "ResultMessage" in (e.error or "")
    ]
    assert synth_errors == []
    # StoppedEvent from abort() is still expected.
    assert any(isinstance(e, StoppedEvent) for e in listener.events)


async def test_sdk_raised_exception_does_not_double_emit(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """If the SDK actually raises (the path the existing except-Exception
    block handles), exactly ONE ErrorEvent must be published — the precise
    one carrying the exception text. The silent-hang guard in finally must
    not also synthesize a second generic ErrorEvent for the same failure."""
    fake_query = _query_raising(RuntimeError("boom: explicit SDK failure"))
    backend, listener, stop_recorder = _make_backend(monkeypatch, fake_query)
    await backend.start(init_data, tool_ctx=object(), hooks=_stop_hooks(stop_recorder))

    with pytest.raises(RuntimeError, match="boom"):
        await backend.run_prompt("hello")

    error_events = [e for e in listener.events if isinstance(e, ErrorEvent)]
    assert len(error_events) == 1
    assert "boom" in error_events[0].error
    # And not the synthetic message from the silent-hang guard.
    assert "ResultMessage" not in error_events[0].error
    assert stop_recorder.reasons == ["error"]
