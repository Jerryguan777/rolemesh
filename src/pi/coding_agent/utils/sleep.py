"""Sleep helper that respects cancellation.

Python port of packages/coding-agent/src/utils/sleep.ts.
"""

from __future__ import annotations

import asyncio
import contextlib


async def sleep(ms: float, cancel_event: asyncio.Event | None = None) -> None:
    """Sleep for *ms* milliseconds, optionally cancelled by an asyncio.Event.

    Raises asyncio.CancelledError if cancel_event is set before the sleep
    completes.
    """
    seconds = ms / 1000.0

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError("Aborted")

    if cancel_event is None:
        await asyncio.sleep(seconds)
        return

    # Race sleep against cancel event
    sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
    cancel_task = asyncio.ensure_future(cancel_event.wait())

    done, pending = await asyncio.wait(
        {sleep_task, cancel_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if cancel_task in done:
        raise asyncio.CancelledError("Aborted")
