"""Central timing instrumentation for startup profiling.

Python port of packages/coding-agent/src/core/timings.ts.
Enable with PI_TIMING=1 environment variable.
"""

from __future__ import annotations

import os
import sys
import time as _time

_ENABLED = os.environ.get("PI_TIMING") == "1"
_timings: list[dict[str, object]] = []
_last_time: float = _time.time() * 1000  # milliseconds


def time(label: str) -> None:
    """Record a timing measurement since the last call."""
    global _last_time
    if not _ENABLED:
        return
    now = _time.time() * 1000
    _timings.append({"label": label, "ms": now - _last_time})
    _last_time = now


def print_timings() -> None:
    """Print all recorded timings to stderr."""
    if not _ENABLED or not _timings:
        return
    print("\n--- Startup Timings ---", file=sys.stderr)
    total = 0.0
    for t in _timings:
        ms = float(t["ms"])  # type: ignore[arg-type]
        total += ms
        print(f"  {t['label']}: {ms:.0f}ms", file=sys.stderr)
    print(f"  TOTAL: {total:.0f}ms", file=sys.stderr)
    print("------------------------\n", file=sys.stderr)
