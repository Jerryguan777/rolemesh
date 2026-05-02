"""Safety Framework background maintenance.

Currently runs one task: the 24-hour TTL on
``safety_decisions.approval_context`` (V2 P1.1 retention). The loop
shape mirrors ``rolemesh.approval.expiry.run_approval_maintenance_loop``
so operators see one family of maintenance tasks with consistent
cadence and log semantics.

Exceptions in the cleanup step are logged and swallowed — the loop
must not die from a transient DB failure, or we would silently
accumulate raw tool_inputs in the audit table.
"""

from __future__ import annotations

import asyncio
import contextlib

from rolemesh.core.logger import get_logger
from rolemesh.db import (
    cleanup_old_safety_approval_contexts,
)

logger = get_logger()


# Once an hour is fine: retention is 24h, we just need to catch up
# within one hour of that threshold. Shorter intervals hammer the DB
# on empty tables; longer ones widen the retention window noticeably.
_MAINTENANCE_INTERVAL_S = 3600.0


async def run_safety_maintenance_loop(
    *,
    retention_hours: int = 24,
    interval_seconds: float = _MAINTENANCE_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Loop that clears stale approval_context rows.

    Run in parallel with the approval maintenance loop; the two don't
    coordinate since they touch different tables, but keeping them
    separate makes it easy to disable one independently in tests.
    """
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            cleared = await cleanup_old_safety_approval_contexts(
                retention_hours=retention_hours
            )
            if cleared:
                logger.info(
                    "safety maintenance: cleared approval_context on "
                    "aged safety_decisions rows",
                    component="safety",
                    retention_hours=retention_hours,
                    rows_cleared=cleared,
                )
        except Exception as exc:  # noqa: BLE001 — loop must survive
            logger.warning(
                "safety maintenance: cleanup step failed",
                component="safety",
                error=str(exc),
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


__all__ = ["run_safety_maintenance_loop"]
