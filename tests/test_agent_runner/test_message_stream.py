"""Tests for MessageStream — the push-based async iterator used by Claude backend.

Focus: concurrency safety, ordering guarantees, race conditions around
the deque + Event pattern. These are the exact behaviors that break
silently if the implementation has timing bugs.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_runner.message_stream import MessageStream


async def _collect_all(stream: MessageStream) -> list[str]:
    """Drain the stream, returning the text content of each message."""
    results: list[str] = []
    async for msg in stream:
        results.append(msg["message"]["content"])
    return results


async def test_single_push_then_end() -> None:
    """Basic: push one message, end, iterate — should yield exactly one."""
    s = MessageStream()
    s.push("hello")
    s.end()
    msgs = await _collect_all(s)
    assert msgs == ["hello"]


async def test_multiple_pushes_before_iteration() -> None:
    """Multiple pushes before iterating — all should be yielded in order."""
    s = MessageStream()
    s.push("a")
    s.push("b")
    s.push("c")
    s.end()
    msgs = await _collect_all(s)
    assert msgs == ["a", "b", "c"]


async def test_end_without_push() -> None:
    """End with nothing pushed — should yield nothing and terminate."""
    s = MessageStream()
    s.end()
    msgs = await _collect_all(s)
    assert msgs == []


async def test_push_after_iteration_starts() -> None:
    """Push arrives while iterator is waiting — should wake up and yield."""
    s = MessageStream()
    received: list[str] = []

    async def consumer() -> None:
        async for msg in s:
            received.append(msg["message"]["content"])

    task = asyncio.create_task(consumer())
    # Let the consumer start waiting
    await asyncio.sleep(0.05)
    assert received == []

    s.push("delayed")
    await asyncio.sleep(0.05)
    assert received == ["delayed"]

    s.end()
    await task


async def test_interleaved_push_and_consume() -> None:
    """Push and consume interleaved — ordering must be preserved."""
    s = MessageStream()
    received: list[str] = []

    async def consumer() -> None:
        async for msg in s:
            received.append(msg["message"]["content"])

    task = asyncio.create_task(consumer())
    for i in range(5):
        s.push(f"msg-{i}")
        await asyncio.sleep(0.02)

    s.end()
    await task
    assert received == [f"msg-{i}" for i in range(5)]


async def test_concurrent_rapid_pushes() -> None:
    """Many pushes from concurrent tasks — nothing should be lost."""
    s = MessageStream()
    n = 50
    received: list[str] = []

    async def consumer() -> None:
        async for msg in s:
            received.append(msg["message"]["content"])

    async def producer(start: int, count: int) -> None:
        for i in range(count):
            s.push(f"{start + i}")
            await asyncio.sleep(0)

    task = asyncio.create_task(consumer())

    # 5 producers each pushing 10 messages
    producers = [asyncio.create_task(producer(i * 10, 10)) for i in range(5)]
    await asyncio.gather(*producers)
    s.end()
    await task

    assert len(received) == n
    # All messages present (order within a producer is guaranteed,
    # but interleaving between producers is not deterministic)
    assert sorted(received, key=int) == [str(i) for i in range(n)]


async def test_end_during_wait_terminates_iterator() -> None:
    """Calling end() while iterator is blocked on wait — should terminate."""
    s = MessageStream()

    async def consumer() -> list[str]:
        result: list[str] = []
        async for msg in s:
            result.append(msg["message"]["content"])
        return result

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    s.end()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == []


async def test_push_then_end_race_condition() -> None:
    """Push and end called nearly simultaneously — push must not be lost.

    This targets the race between event.clear() and event.wait() that
    existed before the re-check fix.
    """
    s = MessageStream()
    received: list[str] = []

    async def consumer() -> None:
        async for msg in s:
            received.append(msg["message"]["content"])

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)

    # Push and end with minimal gap
    s.push("last-message")
    s.end()

    await asyncio.wait_for(task, timeout=1.0)
    assert "last-message" in received


async def test_message_format() -> None:
    """Verify the exact message dict structure pushed into the queue."""
    s = MessageStream()
    s.push("test-content")
    s.end()

    msgs: list[dict] = []
    async for msg in s:
        msgs.append(msg)

    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["type"] == "user"
    assert msg["message"]["role"] == "user"
    assert msg["message"]["content"] == "test-content"
    assert msg["parent_tool_use_id"] is None
    assert msg["session_id"] == ""
