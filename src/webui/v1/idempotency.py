"""In-memory ``idempotency_key`` dedup for ``request.run`` WS messages.

Design intent (01b Open Question 3, locked):

* Client mints a UUID4 per send and passes it as ``idempotency_key``.
* Server keeps a 60-second sliding window *per conversation* keyed
  by the value, mapping it to the ``run_id`` that was minted on
  first sight.
* A second send with the same key inside the window returns the
  same ``run_id`` (and the publish to NATS is skipped).
* A send with the same key *after* the run reached terminal state
  also returns the same ``run_id`` — the client GETs
  ``/api/v1/runs/{id}`` to observe the terminal state. Past the
  60s window, the same key is treated as a fresh request.
* Dedup is scoped to a single conversation: two conversations
  reusing the same key are independent. Cross-conversation leak
  would let an attacker who guessed another conversation's keys
  short-circuit publish-throttling.

Why in-memory and not a KV / DB row: the design pin says
"in-memory dict or KV-cache, not落 DB". The window is short
enough that a webui restart simply loses dedup for a minute, which
is acceptable: a redelivered ``request.run`` after a restart would
re-INSERT and re-publish, and the second run's INSERT either
succeeds (a new run begins) or the conversation's load-bearing
``WHERE status='running'`` gate elsewhere keeps things sane. The
dedup is an optimisation, not a correctness invariant.

A coarse global lock protects the cache from concurrent WS task
race conditions. ``asyncio.Lock`` would serialise all dedup checks
across conversations; we use a per-conversation lock instead so a
busy chat does not stall others.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class _Entry:
    """Cached dedup record for one ``idempotency_key``."""

    run_id: str
    seen_at_monotonic: float


class IdempotencyCache:
    """Per-conversation 60-second sliding window for ``idempotency_key``.

    Concurrency: a per-conversation ``asyncio.Lock`` guards the
    inner dict so two simultaneous ``request.run`` frames on the
    same socket can't both treat the same key as "first seen".
    The outer dict is mutated under a coarse lock; new
    conversation slots are stable references, so the inner-lock
    is the hot path.
    """

    def __init__(self, window_s: float = 60.0) -> None:
        self._window_s = window_s
        self._slots: dict[str, dict[str, _Entry]] = {}
        self._inner_locks: dict[str, asyncio.Lock] = {}
        self._outer_lock = asyncio.Lock()

    async def _get_slot_lock(
        self, conversation_id: str
    ) -> tuple[dict[str, _Entry], asyncio.Lock]:
        """Resolve (or create) the per-conversation slot + lock.

        Acquires the outer lock only once per first-touch on a
        conversation; subsequent calls hit the dict-by-key fast
        path. The asymmetry is fine because slot creation is
        rare relative to dedup probes.
        """
        slot = self._slots.get(conversation_id)
        if slot is not None:
            return slot, self._inner_locks[conversation_id]
        async with self._outer_lock:
            slot = self._slots.get(conversation_id)
            if slot is None:
                slot = {}
                self._slots[conversation_id] = slot
                self._inner_locks[conversation_id] = asyncio.Lock()
            return slot, self._inner_locks[conversation_id]

    async def lookup_or_remember(
        self,
        *,
        conversation_id: str,
        idempotency_key: str,
        run_id_factory_async,
        now_monotonic: float | None = None,
    ) -> tuple[str, bool]:
        """Return ``(run_id, was_cached)``.

        On a cache hit inside the 60s window, returns the cached
        ``run_id`` and ``was_cached=True``; the caller skips its
        NATS publish.

        On a miss (no entry or expired entry), invokes
        ``run_id_factory_async`` to mint a fresh ``run_id``, caches
        the pair, and returns ``was_cached=False``.

        ``run_id_factory_async`` is an awaitable instead of a
        pre-computed value so the create_run / INSERT happens
        *inside* the lock — preventing the race where two frames
        both miss, both INSERT, both write distinct rows.
        """
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        slot, lock = await self._get_slot_lock(conversation_id)
        async with lock:
            self._evict_locked(slot, now)
            existing = slot.get(idempotency_key)
            if existing is not None:
                return existing.run_id, True
            run_id = await run_id_factory_async()
            slot[idempotency_key] = _Entry(
                run_id=run_id, seen_at_monotonic=now
            )
            return run_id, False

    def _evict_locked(
        self, slot: dict[str, _Entry], now: float
    ) -> None:
        """Drop expired entries from a single conversation's slot.

        Called inside the per-conversation lock, so direct mutation
        is safe. Each call is O(n_in_slot) — the cache is bounded
        in practice by the rate of distinct keys per conversation
        per minute, which is small.
        """
        cutoff = now - self._window_s
        expired = [
            k for k, e in slot.items() if e.seen_at_monotonic < cutoff
        ]
        for k in expired:
            del slot[k]

    def _peek(
        self, conversation_id: str, idempotency_key: str
    ) -> _Entry | None:
        """Test-only inspection helper."""
        slot = self._slots.get(conversation_id)
        if slot is None:
            return None
        return slot.get(idempotency_key)


# Module-level singleton — the WS handler imports this directly.
# The cache is per-process; multiple webui replicas each have their
# own. That is acceptable because a re-routed reconnect that lands
# on a different replica would simply create a fresh run — the
# 60-second window is best-effort.
cache = IdempotencyCache()
