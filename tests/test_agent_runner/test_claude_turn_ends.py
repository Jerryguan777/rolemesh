"""Regression: ClaudeBackend.run_prompt must RETURN after a turn settles.

Root cause it guards (slot-leak hunt): ClaudeBackend feeds query() a persistent
MessageStream to support in-turn follow-ups, which puts the SDK in multi-turn
streaming mode — query() stays open reading the input iterator. The "end the
turn = close the input" contract was only honored in abort(); on normal
completion neither stream.end() nor a break happened, so after the last
ResultMessage the async-for blocked forever, run_prompt never returned, and the
NATS bridge never published the batch-final is_final=True marker → the
orchestrator's turn slot leaked until the 7-min watchdog.

Fix: after a ResultMessage, when no follow-up is queued, ClaudeBackend calls
stream.end(). These tests drive run_prompt with a faked SDK query() (the real
claude_agent_sdk only exists inside containers) and assert run_prompt RETURNS —
a wait_for timeout here means the hang regressed.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any

# Stub claude_agent_sdk BEFORE importing claude_backend (container-only dep).
_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type(  # type: ignore[attr-defined]
    "ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None}
)
_fake_sdk.HookMatcher = type(  # type: ignore[attr-defined]
    "HookMatcher", (), {"__init__": lambda self, **kw: setattr(self, "hooks", kw.get("hooks"))}
)
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)


from agent_runner import claude_backend  # noqa: E402
from agent_runner.backend import ResultEvent  # noqa: E402


class ResultMessage:
    """Fake SDK ResultMessage (matched by type(...).__name__ == 'ResultMessage')."""

    def __init__(self, result: str, session_id: str = "sess-1") -> None:
        self.result = result
        self.session_id = session_id
        # No `usage`/`model` attrs → _build_usage_snapshot returns None.


def _make_backend() -> tuple[Any, list[Any]]:
    """A ClaudeBackend wired with just enough state for run_prompt, plus a
    listener that records emitted events."""
    backend = claude_backend.ClaudeBackend()
    backend._init = SimpleNamespace(
        system_prompt=None, mcp_servers=None, user_id=None, is_scheduled_task=False
    )
    backend._mcp_server = object()
    backend._sdk_hooks = {}
    backend._sdk_env = {}
    events: list[Any] = []

    async def listener(event: Any) -> None:
        events.append(event)

    backend.subscribe(listener)
    return backend, events


async def test_run_prompt_returns_when_no_follow_up(monkeypatch: Any) -> None:
    """One reply, no follow-up: the SDK query() blocks on the input iterator
    after the ResultMessage; the fix's stream.end() must let it finish so
    run_prompt returns (pre-fix this hangs forever)."""

    async def fake_query(*, prompt: Any, options: Any) -> Any:
        # Mirror the SDK contract: read each user message from the input
        # iterator and answer it. When the input ends (stream.end()), the
        # async-for terminates and the generator returns.
        async for _ in prompt:
            yield ResultMessage(result="hello back")

    monkeypatch.setattr(claude_backend, "query", fake_query)
    backend, events = _make_backend()

    await asyncio.wait_for(backend.run_prompt("hi"), timeout=2.0)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 1
    assert results[0].text == "hello back"
    assert results[0].is_final is False  # batch-final marker is the bridge's job


async def test_queued_follow_up_answered_then_returns(monkeypatch: Any) -> None:
    """A follow-up queued during the turn must still be answered (has_pending()
    keeps the stream open), and then run_prompt must return — proving the fix
    both preserves in-turn follow-ups and no longer hangs."""

    pushed = False

    async def fake_query(*, prompt: Any, options: Any) -> Any:
        nonlocal pushed
        async for _ in prompt:
            if not pushed:
                # Queue a follow-up BEFORE replying to the first message, so
                # the backend sees has_pending()==True after the first
                # ResultMessage and does NOT end the stream yet.
                pushed = True
                prompt.push("second question")
            yield ResultMessage(result="answer")

    monkeypatch.setattr(claude_backend, "query", fake_query)
    backend, events = _make_backend()

    await asyncio.wait_for(backend.run_prompt("first question"), timeout=2.0)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 2, "the queued follow-up must be answered, not dropped"
