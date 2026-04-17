"""MessageStream — push-based async iterable for Claude SDK user messages.

Extracted into its own module so it can be tested independently
of claude_agent_sdk (which is only available inside containers).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any


class MessageStream:
    """Push-based async iterable for streaming user messages to the SDK.

    Keeps the iterable alive until end() is called, preventing isSingleUserTurn.
    """

    def __init__(self) -> None:
        self._queue: deque[dict[str, Any]] = deque()
        self._event: asyncio.Event = asyncio.Event()
        self._done: bool = False

    def push(self, text: str) -> None:
        self._queue.append(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": "",
            }
        )
        self._event.set()

    def end(self) -> None:
        self._done = True
        self._event.set()

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            while self._queue:
                yield self._queue.popleft()
            if self._done:
                return
            self._event.clear()
            # Re-check after clearing to avoid missed wakeup race
            if self._queue or self._done:
                continue
            await self._event.wait()
