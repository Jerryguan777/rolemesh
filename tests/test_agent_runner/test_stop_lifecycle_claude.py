"""End-to-end Stop hook lifecycle on the Claude backend.

Spec §4.1.8: one run_prompt emits exactly one Stop hook; one abort emits
exactly one Stop hook; both together total 2, not 3 or 4. The reason
field must be "completed" / "aborted" / "error" depending on which path
exited run_prompt.

These tests drive the FULL run_prompt + abort path with a stubbed
claude_agent_sdk.query generator. That reaches code the parity tests
don't cover:

  - run_prompt's try/except/finally ordering and the _aborted flag
  - abort()'s StoppedEvent emission + Stop hook emission sequence
  - interaction between abort() running while run_prompt is alive
    (double-fire risk point #1) vs after run_prompt returned
    (latched-flag risk point)

Mutation-level signals these tests are designed to catch:

  - If run_prompt's finally emits Stop even when aborted -> completion+abort
    overlap => count = 2 from a SINGLE cycle instead of 1 per path.
  - If abort() forgets to emit Stop when called between turns -> count
    = 0 instead of 1 on the quiescent-abort path.
  - If error propagates and the except Exception block doesn't emit Stop
    -> count = 0 on the error path.
  - If reason is hard-coded to "completed" in finally -> aborted turn
    would also produce reason="completed" (wrong).
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator  # noqa: TC003 — used in async-gen return annotation
from dataclasses import dataclass, field
from typing import Any

import pytest

# Same stub pattern as test_claude_abort.py — must go BEFORE importing
# claude_backend so the module's `from claude_agent_sdk import ...` resolves.
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
from agent_runner.hooks import HookRegistry, StopEvent  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


# ClaudeBackend._consume_query dispatches on type(message).__name__ == "SystemMessage"
# etc. Test fakes MUST use those exact class names (no underscore prefix).
@dataclass
class SystemMessage:
    subtype: str = "init"
    data: dict[str, Any] = field(default_factory=lambda: {"session_id": "sid-fake"})


@dataclass
class ResultMessage:
    result: str | None = "ok"
    session_id: str | None = "sid-fake"


def _query_yielding(messages: list[Any]) -> Any:
    def _q(**_kw: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            for m in messages:
                yield m

        return _gen()

    return _q


def _query_raising(exc: Exception) -> Any:
    def _q(**_kw: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            raise exc
            # Unreachable; makes this a real async generator.
            yield  # pragma: no cover

        return _gen()

    return _q


def _query_holding(hold: asyncio.Event, messages: list[Any]) -> Any:
    def _q(**_kw: Any) -> Any:
        async def _gen() -> AsyncIterator[Any]:
            for m in messages:
                yield m
            # Park here until the test releases us. A real in-flight turn
            # sits in an await inside the SDK; abort() must cancel it.
            await hold.wait()

        return _gen()

    return _q


class _StopRecorder:
    """Minimal HookHandler that only records Stop events."""

    def __init__(self) -> None:
        self.events: list[StopEvent] = []

    async def on_stop(self, event: StopEvent) -> None:
        self.events.append(event)


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
        # Required by claude_backend.start() to gate send_message tool
        # registration (see commit 2e63ca7).
        is_scheduled_task: bool = False

    return _Init()


def _make_backend(
    monkeypatch: pytest.MonkeyPatch, fake_query: Any
) -> tuple[Any, _StopRecorder, HookRegistry]:
    monkeypatch.setattr(claude_backend, "query", fake_query, raising=False)

    @dataclass
    class _Opts:
        pass

    monkeypatch.setattr(
        claude_backend, "ClaudeAgentOptions", lambda **_kw: _Opts(), raising=False
    )
    monkeypatch.setattr(
        claude_backend, "HookMatcher", lambda **_kw: object(), raising=False
    )
    monkeypatch.setattr(
        claude_backend,
        "create_rolemesh_mcp_server",
        lambda ctx, **kwargs: object(),
        raising=False,
    )

    backend = claude_backend.ClaudeBackend()
    recorder = _StopRecorder()
    registry = HookRegistry()
    registry.register(recorder)
    return backend, recorder, registry


# ---------------------------------------------------------------------------
# Happy path: run_prompt returns normally
# ---------------------------------------------------------------------------


async def test_run_prompt_completion_emits_one_stop_completed(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """Happy path: query yields init + result then ends cleanly ->
    exactly one Stop(reason="completed"), no aborted/error emission."""
    backend, recorder, registry = _make_backend(
        monkeypatch, _query_yielding([SystemMessage(), ResultMessage()])
    )
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    await backend.run_prompt("hi")

    assert len(recorder.events) == 1, (
        f"expected exactly one Stop emission, got {len(recorder.events)}: "
        f"{[e.reason for e in recorder.events]}"
    )
    assert recorder.events[0].reason == "completed"
    assert recorder.events[0].session_id == "sid-fake"


# ---------------------------------------------------------------------------
# Abort during active run_prompt
# ---------------------------------------------------------------------------


async def test_abort_during_run_prompt_emits_one_stop_aborted(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """run_prompt is parked waiting for more SDK messages; abort() fires.
    Total Stop emissions = 1, reason = "aborted". Crucially the
    run_prompt finally MUST NOT also emit Stop(completed) — that would
    make count = 2 for one user turn."""
    hold = asyncio.Event()
    backend, recorder, registry = _make_backend(
        monkeypatch, _query_holding(hold, [SystemMessage()])
    )
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    prompt_task = asyncio.create_task(backend.run_prompt("Q1"))

    # Wait for the query task to actually start.
    for _ in range(100):
        await asyncio.sleep(0)
        if backend._query_task is not None and not backend._query_task.done():
            break
    assert backend._query_task is not None

    await backend.abort()
    hold.set()  # let the generator end so prompt_task can unwind
    await prompt_task

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["aborted"], (
        f"abort during active turn must produce exactly ['aborted'], got {reasons}"
    )


# ---------------------------------------------------------------------------
# Abort between turns (no active run_prompt)
# ---------------------------------------------------------------------------


async def test_abort_between_turns_still_emits_one_stop_aborted(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """Stop button pressed when nothing is running. The backend still
    goes through abort() (UI double-click, abort races etc.). Must emit
    exactly one Stop(aborted) — not zero (latched _aborting bug) or two
    (double fire via some defensive retry)."""
    backend, recorder, registry = _make_backend(
        monkeypatch, _query_yielding([])
    )
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    await backend.abort()

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["aborted"], f"expected ['aborted'], got {reasons}"


# ---------------------------------------------------------------------------
# run_prompt then abort (two user-visible events, two Stop emissions)
# ---------------------------------------------------------------------------


async def test_completion_then_abort_totals_two_distinct_reasons(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """A full completed turn followed by a between-turn abort = 2 Stop
    emissions with distinct reasons. This is the spec's "= 2, not 3 or 4"
    regression shape."""
    backend, recorder, registry = _make_backend(
        monkeypatch, _query_yielding([SystemMessage(), ResultMessage()])
    )
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    await backend.run_prompt("Q1")
    await backend.abort()

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["completed", "aborted"], (
        f"expected ['completed', 'aborted'], got {reasons}"
    )


# ---------------------------------------------------------------------------
# Error path: query raises unhandled exception
# ---------------------------------------------------------------------------


async def test_run_prompt_error_emits_one_stop_error(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """SDK query raises -> run_prompt re-raises (caller's job to handle)
    AND Stop(reason="error") fires exactly once in the finally before
    re-raise. No "completed" leaks through."""
    backend, recorder, registry = _make_backend(
        monkeypatch, _query_raising(RuntimeError("SDK crashed"))
    )
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    with pytest.raises(RuntimeError, match="SDK crashed"):
        await backend.run_prompt("Q1")

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["error"], (
        f"error path must emit exactly ['error'], got {reasons}"
    )


# ---------------------------------------------------------------------------
# Two back-to-back completions must produce two Stop(completed) events
# ---------------------------------------------------------------------------


async def test_two_sequential_prompts_emit_two_completions(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """Mutation target: accidentally caching the Stop emission (e.g. only
    firing on first turn or skipping when _aborting is False-but-set)
    would show up as 1 emission for 2 prompts."""
    backend, recorder, registry = _make_backend(
        monkeypatch, _query_yielding([SystemMessage(), ResultMessage()])
    )
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    await backend.run_prompt("Q1")
    await backend.run_prompt("Q2")

    reasons = [e.reason for e in recorder.events]
    assert reasons == ["completed", "completed"], (
        f"two prompts should emit two completed stops, got {reasons}"
    )


# ---------------------------------------------------------------------------
# Stop handler crash must NOT propagate out of abort()/run_prompt
# ---------------------------------------------------------------------------


async def test_crashing_stop_handler_does_not_break_run_prompt(
    monkeypatch: pytest.MonkeyPatch, init_data: Any
) -> None:
    """emit_stop is fail-safe, but the backend also has a defensive
    try/except around the emit. Both together must keep run_prompt
    from raising when a Stop handler explodes."""

    class _Boom:
        async def on_stop(self, event: StopEvent) -> None:
            raise RuntimeError("metrics sink down")

    backend, _recorder, registry = _make_backend(
        monkeypatch, _query_yielding([SystemMessage(), ResultMessage()])
    )
    registry.register(_Boom())
    await backend.start(init_data, tool_ctx=object(), hooks=registry)

    # Must not raise.
    await backend.run_prompt("Q1")
    await backend.abort()
