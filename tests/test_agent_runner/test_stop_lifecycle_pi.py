"""End-to-end Stop hook lifecycle on the Pi backend.

Mirror of test_stop_lifecycle_claude.py. The Pi backend cancels
cooperatively (asyncio.Event signal) rather than preemptively
(task.cancel()), but the observable Stop-hook contract is identical:

  - run_prompt returns normally       -> 1x Stop(reason="completed")
  - run_prompt raises                 -> 1x Stop(reason="error")
  - abort() during run_prompt         -> 1x Stop(reason="aborted")
  - abort() between turns             -> 1x Stop(reason="aborted")
  - completion followed by abort      -> ["completed", "aborted"]

Pi's AgentSession has a much larger surface than Claude SDK's
`query()`, so we replace `self._session` on the backend instance
directly rather than stubbing a module import. The tests never call
start() because start() spins up an AgentSession; we're verifying
run_prompt/abort lifecycle hooks, not construction.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_runner import pi_backend
from agent_runner.hooks import HookRegistry, StopEvent


class _StopRecorder:
    def __init__(self) -> None:
        self.events: list[StopEvent] = []

    async def on_stop(self, event: StopEvent) -> None:
        self.events.append(event)


class _FakeAgentSession:
    """Stand-in for pi.coding_agent.core.agent_session.AgentSession.

    Only the methods PiBackend.run_prompt/abort actually touch are
    implemented. The default is a fast happy-path: prompt() returns
    immediately. Tests can pass hold/raise overrides to simulate
    stuck turns or errors.
    """

    def __init__(
        self,
        *,
        hold: asyncio.Event | None = None,
        prompt_raises: Exception | None = None,
    ) -> None:
        self.prompt_calls: list[tuple[str, dict[str, Any]]] = []
        self.abort_called = 0
        self._hold = hold
        self._prompt_raises = prompt_raises
        self.is_streaming = False

    async def prompt(self, text: str, **kwargs: Any) -> None:
        self.prompt_calls.append((text, kwargs))
        if self._prompt_raises is not None:
            raise self._prompt_raises
        if self._hold is not None:
            await self._hold.wait()

    async def abort(self) -> None:
        self.abort_called += 1
        # Release any parked prompt so the prompt_task can unwind.
        if self._hold is not None:
            self._hold.set()


def _make_backend(
    session: _FakeAgentSession,
) -> tuple[pi_backend.PiBackend, _StopRecorder, HookRegistry]:
    backend = pi_backend.PiBackend()
    recorder = _StopRecorder()
    registry = HookRegistry()
    registry.register(recorder)
    backend._hooks = registry
    backend._session = session  # type: ignore[assignment]
    backend._session_file = "sid-fake"
    return backend, recorder, registry


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_run_prompt_completion_emits_one_stop_completed() -> None:
    backend, recorder, _ = _make_backend(_FakeAgentSession())

    await backend.run_prompt("hi")

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["completed"], f"expected ['completed'], got {reasons}"
    assert recorder.events[0].session_id == "sid-fake"


async def test_two_sequential_prompts_emit_two_completions() -> None:
    backend, recorder, _ = _make_backend(_FakeAgentSession())

    await backend.run_prompt("Q1")
    await backend.run_prompt("Q2")

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["completed", "completed"], reasons


# ---------------------------------------------------------------------------
# Abort paths
# ---------------------------------------------------------------------------


async def test_abort_during_run_prompt_emits_one_stop_aborted() -> None:
    """run_prompt is parked inside session.prompt waiting on `hold`.
    abort() releases the hold AND must emit Stop(aborted). Crucially the
    finally of run_prompt MUST NOT also fire Stop(completed) — that's
    the 'count = 2 for one user turn' failure mode."""
    hold = asyncio.Event()
    session = _FakeAgentSession(hold=hold)
    backend, recorder, _ = _make_backend(session)

    prompt_task = asyncio.create_task(backend.run_prompt("Q1"))

    # Wait for session.prompt to start.
    for _ in range(100):
        await asyncio.sleep(0)
        if session.prompt_calls:
            break
    assert session.prompt_calls

    await backend.abort()
    await prompt_task

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["aborted"], f"expected ['aborted'], got {reasons}"
    assert session.abort_called == 1


async def test_abort_between_turns_emits_one_stop_aborted() -> None:
    """Quiescent abort: no prompt running. Still produces one Stop(aborted)."""
    backend, recorder, _ = _make_backend(_FakeAgentSession())

    await backend.abort()

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["aborted"], reasons


async def test_completion_then_abort_totals_two_distinct_reasons() -> None:
    backend, recorder, _ = _make_backend(_FakeAgentSession())

    await backend.run_prompt("Q1")
    await backend.abort()

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["completed", "aborted"], reasons


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


async def test_run_prompt_error_emits_one_stop_error() -> None:
    backend, recorder, _ = _make_backend(
        _FakeAgentSession(prompt_raises=RuntimeError("provider down"))
    )

    with pytest.raises(RuntimeError, match="provider down"):
        await backend.run_prompt("Q1")

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["error"], reasons


# ---------------------------------------------------------------------------
# Stop handler exceptions must not break the backend
# ---------------------------------------------------------------------------


async def test_crashing_stop_handler_does_not_break_pi_backend() -> None:
    class _Boom:
        async def on_stop(self, event: StopEvent) -> None:
            raise RuntimeError("audit db down")

    session = _FakeAgentSession()
    backend, _, registry = _make_backend(session)
    registry.register(_Boom())

    # None of these should raise.
    await backend.run_prompt("Q1")
    await backend.abort()


# ---------------------------------------------------------------------------
# UserPromptSubmit block: Stop still emits once as "completed"
# ---------------------------------------------------------------------------


async def test_user_prompt_block_still_emits_one_completed_stop() -> None:
    """A handler blocks the prompt entirely — session.prompt is never
    called. The Stop hook still fires once (reason='completed') because
    from the orchestrator's view run_prompt cleanly finished; it just
    produced no assistant reply. Not emitting Stop would strand the UI
    waiting for a 'stopping' transition that never arrives."""

    class _Blocker:
        async def on_user_prompt_submit(self, event: Any) -> Any:
            from agent_runner.hooks import UserPromptVerdict

            return UserPromptVerdict(block=True, reason="denied")

    session = _FakeAgentSession()
    backend, recorder, registry = _make_backend(session)
    registry.register(_Blocker())

    await backend.run_prompt("bad-input")

    assert session.prompt_calls == [], "session.prompt must not be invoked"
    reasons = [e.reason for e in recorder.events]
    assert reasons == ["completed"], reasons
