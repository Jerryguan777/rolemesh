"""Per-delegation throttle bucket for child-chip progress events.

Frontdesk v1.5 surfaces target-container progress to the parent web UI
as a sub-chip rendered beneath the parent agent's status chip. Tool-use
events and ``running`` heartbeats can arrive in tight bursts (multiple
per second for chained tool calls); we cap them to one emit per
``_THROTTLE_WINDOW_S`` per ``phase`` so the WebSocket doesn't get
hammered and the UI doesn't flicker.

The bucket is per-delegation (one instance per call to the delegation
handler) — there is no cross-delegation contention. A simple in-memory
dict keyed by phase ("status" / "tool_use") is enough; phases beyond
the v1.5 set degrade gracefully (each new phase gets its own slot on
first hit).

**Last-flush guarantee**: within a window, newer payloads OVERWRITE
older deferred payloads (we only keep the latest because the UI
displays a single line per phase). On delegation close, the handler
MUST call ``flush_all()`` to emit any still-deferred payloads —
otherwise a tool-use that landed in the last 500ms before close would
be dropped silently.
"""

from __future__ import annotations

import time

# 500ms window matches typical UI perception threshold (sub-second
# updates feel "live", coalesced bursts of <2/sec land at the same
# subjective speed as a 2 Hz refresh). Tunable per deployment if
# observability load demands it.
_THROTTLE_WINDOW_S = 0.5


class ChipThrottleBucket:
    """Per-phase throttle with last-write-wins deferred payloads.

    Use one instance per delegation (per child container's event
    stream). After the delegation terminates, call ``flush_all()`` to
    drain any deferred payloads before emitting ``close``.

    Not thread-safe (single asyncio task drives each delegation's
    ``_on_output`` callback; no cross-task access).
    """

    def __init__(self) -> None:
        # phase → (last_emit_monotonic, deferred_payload_or_None)
        self._state: dict[str, tuple[float, dict[str, object] | None]] = {}

    def should_emit(
        self, phase: str, payload: dict[str, object]
    ) -> tuple[bool, dict[str, object] | None]:
        """Decide whether to emit ``payload`` now, or defer it.

        Returns ``(emit_now, prior_deferred)``:
          - If ``emit_now`` is True, the caller emits ``payload`` AND
            ``prior_deferred`` (if not None) — the prior payload was
            held back within the window and should be flushed first so
            its order is preserved.
          - If ``emit_now`` is False, the caller drops the emit; the
            bucket has stored ``payload`` as the new deferred. The
            previous deferred (if any) is dropped — last-write-wins,
            because the UI shows only the latest line per phase.
        """
        now = time.monotonic()
        last_ts, deferred = self._state.get(phase, (0.0, None))
        if now - last_ts >= _THROTTLE_WINDOW_S:
            # Window elapsed — emit now and clear deferred slot.
            self._state[phase] = (now, None)
            return True, deferred
        # Still in window — defer; newer payload overwrites older.
        self._state[phase] = (last_ts, payload)
        return False, None

    def flush_all(self) -> list[tuple[str, dict[str, object]]]:
        """Drain all deferred payloads. Call at delegation close.

        Returns a list of ``(phase, payload)`` pairs in deterministic
        phase order (dict iteration order = insertion order in 3.7+).
        After flush, all slots have their deferred cleared but the
        last-emit timestamps remain — irrelevant since the bucket is
        about to be discarded.
        """
        out: list[tuple[str, dict[str, object]]] = []
        for phase, (_, deferred) in self._state.items():
            if deferred is not None:
                out.append((phase, deferred))
        for phase, (ts, _) in list(self._state.items()):
            self._state[phase] = (ts, None)
        return out
