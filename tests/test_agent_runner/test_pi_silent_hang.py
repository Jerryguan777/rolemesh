"""Pi backend: upstream LLM errors must surface as ErrorEvent.

Regression for the silent-hang class of bugs. When the LLM HTTP call
fails (egress 403, 5xx, timeout, max_turns), Pi does NOT raise out of
``session.prompt()`` — it converts the failure into an in-stream
``ErrorEvent`` (``src/pi/agent/proxy.py:413-418``), which becomes an
``AssistantMessage`` with ``stop_reason="error"`` (``agent_loop.py:297-305``)
and is emitted as a ``PromptTurnCompleteEvent`` carrying that message
(``agent_loop.py:188-192``).

Pre-fix, ``_handle_event`` only looked at message text. Error
PromptTurnCompletes carry no text content (only ``error_message``),
so ``if text:`` failed and the event was silently dropped. ``run_prompt``
then returned with no exception, ``_emit_stop("completed")`` was sent,
and the orchestrator believed the turn succeeded — user saw no reply.

These tests pin the new translation contract:

  1. ``stop_reason="error"`` with ``error_message`` → ErrorEvent emitted
     carrying that error_message; NO ResultEvent emitted.
  2. ``stop_reason="error"`` without ``error_message`` → ErrorEvent emitted
     with the documented fallback string; NO ResultEvent emitted.
  3. ``stop_reason="aborted"`` → no ErrorEvent and no ResultEvent
     (abort() owns the StoppedEvent on this path; emitting either here
     would race the abort flow).
  4. ``stop_reason="stop"`` with text content → ResultEvent emitted
     (regression guard for the healthy-turn path).
  5. ``stop_reason="stop"`` with no text → no event (legitimately silent
     turn — agent answered with tool_use only and then ended).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# Pi backend's import chain pulls in third-party deps gated behind extras.
# In a minimal dev venv they may be absent — skip rather than fail-collect.
pi_backend = pytest.importorskip(
    "agent_runner.pi_backend",
    reason="Pi backend deps not installed in this env",
)

from agent_runner.backend import (  # noqa: E402
    BackendEvent,
    ErrorEvent,
    ResultEvent,
    ToolUseEvent,
)
from pi.agent.types import PromptTurnCompleteEvent  # noqa: E402
from pi.ai.types import AssistantMessage, TextContent  # noqa: E402

PiBackend = pi_backend.PiBackend


def _backend_with_recorder() -> tuple[Any, list[BackendEvent]]:
    """Build a PiBackend wired to a recording listener.

    No Pi session is created — _handle_event is exercised in isolation.
    """
    backend = PiBackend()
    emitted: list[BackendEvent] = []

    async def _record(event: BackendEvent) -> None:
        emitted.append(event)

    backend.subscribe(_record)
    return backend, emitted


def _msg(
    *,
    stop_reason: str | None,
    error_message: str | None = None,
    text: str = "",
) -> AssistantMessage:
    """Construct an AssistantMessage shaped like the ones agent_loop produces."""
    return AssistantMessage(
        api="anthropic-completions",
        provider="test",
        model="test-model",
        content=[TextContent(text=text)] if text else [],
        stop_reason=stop_reason,  # type: ignore[arg-type]
        error_message=error_message,
    )


async def _drain(backend: Any) -> None:
    """Wait for fire-and-forget _schedule_emit tasks to land on the listener.

    _handle_event uses _schedule_emit which posts onto loop.create_task.
    A bare assertion right after the call would race the unscheduled task.
    """
    if backend._bg_tasks:
        await asyncio.gather(*list(backend._bg_tasks), return_exceptions=True)
    # One extra yield in case _emit awaits inside the listener.
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Error path — the actual bug fix
# ---------------------------------------------------------------------------


async def test_error_stop_reason_with_no_partial_text_emits_only_error_event() -> None:
    """The clean upstream-failure case: stream ended in error before any text
    was produced (e.g. egress 403 on the very first byte). content is empty,
    so the backend must emit ONLY an ErrorEvent — no synthetic empty
    ResultEvent posing as an assistant reply."""
    backend, emitted = _backend_with_recorder()
    msg = _msg(
        stop_reason="error",
        error_message="Proxy error: 403 Forbidden — domain not in allowlist",
    )

    backend._handle_event(PromptTurnCompleteEvent(message=msg))
    await _drain(backend)

    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, ErrorEvent)
    assert event.error == "Proxy error: 403 Forbidden — domain not in allowlist"
    assert not any(isinstance(e, ResultEvent) for e in emitted)


async def test_error_stop_reason_with_partial_text_emits_both_result_then_error() -> None:
    """The mid-stream-failure case (the most common upstream timeout pattern):
    LLM has streamed some text, then connection drops. proxy.py reuses the
    same partial AssistantMessage when it yields ErrorEvent — so the
    PromptTurnComplete message has stop_reason="error" AND non-empty content.

    Both must surface: the partial reply so the user sees what the model
    actually said, and the error so the orchestrator records the failure.
    Order is load-bearing — ResultEvent before ErrorEvent so a UI streaming
    these in order shows the text first, then the error indicator."""
    backend, emitted = _backend_with_recorder()
    msg = _msg(
        stop_reason="error",
        error_message="upstream connection reset",
        text="I think the answer is",
    )

    backend._handle_event(PromptTurnCompleteEvent(message=msg))
    await _drain(backend)

    assert len(emitted) == 2
    result, err = emitted
    assert isinstance(result, ResultEvent)
    assert result.text == "I think the answer is"
    assert result.is_final is False
    assert isinstance(err, ErrorEvent)
    assert err.error == "upstream connection reset"


async def test_error_with_no_error_message_uses_fallback() -> None:
    """If error_message is None (e.g. a Pi internal codepath neglects to set
    it), we must still emit a real ErrorEvent rather than ``ErrorEvent(error=None)``
    which would surface as the literal string 'None' to the user."""
    backend, emitted = _backend_with_recorder()
    msg = _msg(stop_reason="error", error_message=None)

    backend._handle_event(PromptTurnCompleteEvent(message=msg))
    await _drain(backend)

    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, ErrorEvent)
    assert event.error  # truthy fallback string
    assert "None" not in event.error
    assert "error" in event.error.lower()


# ---------------------------------------------------------------------------
# Aborted path — must NOT compete with abort()'s own StoppedEvent flow
# ---------------------------------------------------------------------------


async def test_aborted_stop_reason_emits_nothing() -> None:
    """abort() emits its own StoppedEvent and rewinds session state. If
    _handle_event also emits an ErrorEvent on the racing PromptTurnComplete,
    the orchestrator gets two competing terminal signals for one turn."""
    backend, emitted = _backend_with_recorder()
    msg = _msg(stop_reason="aborted", error_message="aborted by user")

    backend._handle_event(PromptTurnCompleteEvent(message=msg))
    await _drain(backend)

    assert emitted == []


# ---------------------------------------------------------------------------
# Healthy paths — regression guards so the fix doesn't break normal turns
# ---------------------------------------------------------------------------


async def test_normal_stop_with_text_emits_result_event() -> None:
    """Baseline: stop_reason="stop" with text content remains a ResultEvent.
    Without this guard the error-path code could accidentally swallow real
    replies."""
    backend, emitted = _backend_with_recorder()
    msg = _msg(stop_reason="stop", text="hello, world")

    backend._handle_event(PromptTurnCompleteEvent(message=msg))
    await _drain(backend)

    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, ResultEvent)
    assert event.text == "hello, world"
    assert event.is_final is False


async def test_normal_stop_with_no_text_emits_nothing() -> None:
    """Legitimately silent turn: agent's final assistant message had no text
    (e.g. the run ended on a tool_use only). Must NOT synthesize an
    ErrorEvent — that would be a false positive for the silent-hang fix.
    """
    backend, emitted = _backend_with_recorder()
    msg = _msg(stop_reason="stop", text="")

    backend._handle_event(PromptTurnCompleteEvent(message=msg))
    await _drain(backend)

    assert emitted == []


async def test_message_missing_emits_nothing() -> None:
    """Defensive: PromptTurnComplete with no message attribute must not
    crash the handler. Pi types make message non-optional but a future
    refactor or a partial event injected by tests shouldn't hang the
    backend."""
    backend, emitted = _backend_with_recorder()

    class _BareEvent(PromptTurnCompleteEvent):
        pass

    bare = _BareEvent()
    # Force message to None to simulate the defensive branch.
    object.__setattr__(bare, "message", None)

    backend._handle_event(bare)
    await _drain(backend)

    assert emitted == []


# ---------------------------------------------------------------------------
# Other event types unaffected
# ---------------------------------------------------------------------------


async def test_tool_execution_start_event_still_emits_tool_use() -> None:
    """ToolExecutionStartEvent translation is unrelated to the error path,
    but lives in the same _handle_event body. Pinning it here so the
    error-branch refactor doesn't accidentally short-circuit the tool path."""
    from pi.agent.types import ToolExecutionStartEvent

    backend, emitted = _backend_with_recorder()
    backend._handle_event(
        ToolExecutionStartEvent(
            tool_call_id="t1", tool_name="bash", args={"command": "ls"}
        )
    )
    await _drain(backend)

    assert len(emitted) == 1
    event = emitted[0]
    assert isinstance(event, ToolUseEvent)
    assert event.tool == "bash"
