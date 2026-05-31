"""Safety Framework background maintenance.

The loop currently has no periodic work of its own. It is kept as a
stable wiring point (started from ``rolemesh.main``) so future safety
retention/cleanup tasks can be added here without re-threading a new
background task through the orchestrator lifecycle.

Exceptions in any cleanup step are logged and swallowed — the loop
must not die from a transient DB failure.
"""

from __future__ import annotations

import asyncio
import contextlib

from rolemesh.core.logger import get_logger

logger = get_logger()


# Once an hour is fine for periodic maintenance: shorter intervals
# hammer the DB on empty tables; longer ones widen any future
# retention window noticeably.
_MAINTENANCE_INTERVAL_S = 3600.0


async def run_safety_maintenance_loop(
    *,
    retention_hours: int = 24,
    interval_seconds: float = _MAINTENANCE_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periodic safety maintenance loop.

    Currently a no-op idle loop (no periodic cleanup is required);
    retained so future retention tasks can hook in here. Honors
    ``stop_event`` for clean shutdown.
    """
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


__all__ = ["run_safety_maintenance_loop"]
