"""Async event stream for streaming LLM responses.

Ported from packages/ai/src/utils/event-stream.ts.
Uses asyncio.Queue for push-based async iteration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from pi.ai.types import AssistantMessage, AssistantMessageEvent, DoneEvent, ErrorEvent


class EventStream[T, R]:
    """Push-based async iterable event stream with a final result."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[T | None] = asyncio.Queue()
        self._done = False
        self._result_future: asyncio.Future[R] = asyncio.get_event_loop().create_future()

    def _is_complete(self, event: T) -> bool:
        raise NotImplementedError

    def _extract_result(self, event: T) -> R:
        raise NotImplementedError

    def push(self, event: T) -> None:
        if self._done:
            return
        if self._is_complete(event):
            self._done = True
            if not self._result_future.done():
                self._result_future.set_result(self._extract_result(event))
        self._queue.put_nowait(event)

    def end(self, result: R | None = None) -> None:
        self._done = True
        if result is not None and not self._result_future.done():
            self._result_future.set_result(result)
        self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[T]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def result(self) -> R:
        return await self._result_future


class AssistantMessageEventStream(EventStream[AssistantMessageEvent, AssistantMessage]):
    """Event stream specialized for assistant message events."""

    def _is_complete(self, event: AssistantMessageEvent) -> bool:
        return isinstance(event, (DoneEvent, ErrorEvent))

    def _extract_result(self, event: AssistantMessageEvent) -> AssistantMessage:
        if isinstance(event, DoneEvent):
            return event.message
        if isinstance(event, ErrorEvent):
            return event.error
        raise ValueError("Unexpected event type for final result")


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    """Factory function for AssistantMessageEventStream (for use in extensions)."""
    return AssistantMessageEventStream()
