"""Approval maintenance loop.

Combines expiry scans and stuck-row reconciliation into a single
background task. A single task is simpler than two: both jobs run at
the same cadence, share one DB pool, and cannot deadlock against each
other if serialized on a single coroutine.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from .engine import ApprovalEngine

logger = get_logger()

# Maintenance cadence. Long enough that the DB isn't hammered by scans
# of empty tables (zero-policy tenants) and short enough that a missed
# approval.decided publish turns around inside a minute
# (reconcile_stuck_requests' 60s grace + this interval).
_MAINTENANCE_INTERVAL_S = 30.0


async def run_approval_maintenance_loop(
    engine: ApprovalEngine,
    *,
    interval_seconds: float = _MAINTENANCE_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run expire + reconcile in a loop until stop_event is set.

    Exceptions in either step are logged and swallowed — the loop must
    not die from a transient DB failure, or we would silently stop
    expiring approvals and never surface stuck execution.
    """
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await engine.expire_stale_requests()
        except Exception as exc:  # noqa: BLE001 — loop must survive
            logger.warning("approval maintenance: expire step failed", error=str(exc))
        try:
            await engine.reconcile_stuck_requests()
        except Exception as exc:  # noqa: BLE001 — loop must survive
            logger.warning(
                "approval maintenance: reconcile step failed", error=str(exc)
            )
        # Sleep with cancellation awareness so a graceful shutdown is snappy.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


__all__ = ["run_approval_maintenance_loop"]
