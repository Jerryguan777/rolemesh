"""Verify ``TracingHookHandler`` is safe to register unconditionally:
runs cleanly with the OTel SDK absent (or missing endpoint), tolerates
missing ``tool_call_id``, doesn't leak open spans on stop.

Doesn't try to assert the *content* of emitted spans — that path
needs a live tracer with an in-memory exporter, which is heavier than
the spike warrants. The walkthrough in ``docs/observability/spike.md``
covers that against a real Langfuse.
"""

from __future__ import annotations

import pytest

from agent_runner.hooks.events import (
    StopEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from agent_runner.hooks.handlers import TracingHookHandler


@pytest.mark.asyncio
async def test_pre_post_round_trip_does_not_raise() -> None:
    """Normal happy path — pre opens a span, post closes it. No
    exceptions, no leaked open spans.
    """
    handler = TracingHookHandler()
    pre = ToolCallEvent(
        tool_name="Bash", tool_input={"cmd": "ls"}, tool_call_id="call_1"
    )
    post = ToolResultEvent(
        tool_name="Bash",
        tool_input={"cmd": "ls"},
        tool_result="hello\n",
        is_error=False,
        tool_call_id="call_1",
    )
    await handler.on_pre_tool_use(pre)
    await handler.on_post_tool_use(post)
    assert handler._open_spans == {}


@pytest.mark.asyncio
async def test_failure_path_closes_span() -> None:
    """``on_post_tool_use_failure`` releases the open span just like
    ``on_post_tool_use`` does on the success path.
    """
    handler = TracingHookHandler()
    pre = ToolCallEvent(
        tool_name="Bash", tool_input={"cmd": "ls"}, tool_call_id="call_2"
    )
    fail = ToolResultEvent(
        tool_name="Bash",
        tool_input={"cmd": "ls"},
        tool_result="ENOENT",
        is_error=True,
        tool_call_id="call_2",
    )
    await handler.on_pre_tool_use(pre)
    await handler.on_post_tool_use_failure(fail)
    assert handler._open_spans == {}


@pytest.mark.asyncio
async def test_post_without_pre_is_silent() -> None:
    """Defensive: a stray post event that has no matching pre must be
    a no-op rather than KeyError. Backends should not produce this in
    steady state but the cost of guarding is one ``dict.pop(default)``.
    """
    handler = TracingHookHandler()
    post = ToolResultEvent(
        tool_name="Bash",
        tool_input={},
        tool_result="?",
        tool_call_id="never_opened",
    )
    await handler.on_post_tool_use(post)
    await handler.on_post_tool_use_failure(post)
    assert handler._open_spans == {}


@pytest.mark.asyncio
async def test_stop_force_ends_leftover_spans() -> None:
    """If a backend drops a post event, ``on_stop`` cleans the slate
    so the next run starts with no leaked open spans.
    """
    handler = TracingHookHandler()
    await handler.on_pre_tool_use(
        ToolCallEvent(tool_name="Bash", tool_input={}, tool_call_id="leaked")
    )
    assert handler._open_spans  # pre opened it
    await handler.on_stop(StopEvent(reason="aborted"))
    assert handler._open_spans == {}
