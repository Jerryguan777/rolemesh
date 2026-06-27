"""MessageStream — push-based async iterable for Claude SDK user messages.

Extracted into its own module so it can be tested independently
of claude_agent_sdk (which is only available inside containers).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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

    def has_pending(self) -> bool:
        """True while user messages are queued but not yet consumed by the SDK.

        Used by the backend to decide whether a turn is fully answered: after a
        ResultMessage, an empty queue means there is no follow-up to keep the
        multi-turn stream open for, so the input can be ended. ``__aiter__``
        drains the queue before honoring ``end()``, so a follow-up that races
        in right after this check is still delivered, never lost.
        """
        return bool(self._queue)

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
