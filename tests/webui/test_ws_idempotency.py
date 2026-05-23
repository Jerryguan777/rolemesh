"""01b §PR2 — pinned test for the WS ``request.run`` idempotency cache.

Three contracts pinned here (Open Question 3, locked at session
prompt):

1. **Cache hit inside the 60s window returns the same run_id and
   *does NOT* invoke the run_id factory** — guarantees no double
   INSERT into ``runs`` and no duplicate NATS publish on
   redelivery.
2. **Same key after the 60s window is treated as a new request** —
   the factory runs again and a fresh run_id lands.
3. **Per-conversation scoping** — the same key in conversation A
   does not block the same key in conversation B.

These are property-level claims, not implementation mirrors. Each
test names what is being protected ("no double publish") rather
than which dict key gets set.
"""

from __future__ import annotations

import asyncio

import pytest

from webui.v1.idempotency import IdempotencyCache


@pytest.mark.asyncio
async def test_cache_hit_returns_same_run_id_and_skips_factory() -> None:
    cache = IdempotencyCache(window_s=60.0)
    calls: list[int] = []

    async def factory() -> str:
        calls.append(1)
        return "run-1"

    r1, cached1 = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="k1",
        run_id_factory_async=factory,
    )
    assert (r1, cached1) == ("run-1", False)

    r2, cached2 = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="k1",
        run_id_factory_async=factory,
    )
    assert (r2, cached2) == ("run-1", True)
    assert len(calls) == 1, (
        "factory must NOT be invoked on a cache hit — that would "
        "double-INSERT into ``runs`` and double-publish to NATS"
    )


@pytest.mark.asyncio
async def test_cache_miss_after_window_expiry_remints_run_id() -> None:
    """The 60s sliding window: a re-send past the window is a fresh request.

    Tests pass a controlled ``now_monotonic`` instead of waiting 60
    seconds so the suite doesn't take a minute per assertion.
    """
    cache = IdempotencyCache(window_s=60.0)
    seq = iter(["run-1", "run-2"])

    async def factory() -> str:
        return next(seq)

    r1, _ = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="k1",
        run_id_factory_async=factory,
        now_monotonic=0.0,
    )
    assert r1 == "run-1"

    # 61 seconds later — past the 60s window
    r2, cached = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="k1",
        run_id_factory_async=factory,
        now_monotonic=61.0,
    )
    assert r2 == "run-2"
    assert cached is False


@pytest.mark.asyncio
async def test_per_conversation_scoping_does_not_leak() -> None:
    """Same key in two conversations -> two distinct run_ids, both fresh."""
    cache = IdempotencyCache(window_s=60.0)
    seq = iter(["run-A", "run-B"])

    async def factory() -> str:
        return next(seq)

    rA, cachedA = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="shared-key",
        run_id_factory_async=factory,
    )
    rB, cachedB = await cache.lookup_or_remember(
        conversation_id="conv-B",
        idempotency_key="shared-key",
        run_id_factory_async=factory,
    )
    assert (rA, cachedA) == ("run-A", False)
    assert (rB, cachedB) == ("run-B", False)


@pytest.mark.asyncio
async def test_concurrent_first_sends_only_invoke_factory_once() -> None:
    """Two simultaneous ``request.run`` frames on one conv must collapse.

    Without the per-conversation lock, both frames could miss the
    cache, both call the factory, and we'd end up with two run rows
    + two NATS publishes. The lock makes the dedup atomic.
    """
    cache = IdempotencyCache(window_s=60.0)
    factory_calls: list[int] = []
    factory_event = asyncio.Event()

    async def slow_factory() -> str:
        # Hold the factory open until both contenders are queued
        # behind the lock — the second contender must observe the
        # cached run_id rather than minting its own.
        factory_calls.append(1)
        await factory_event.wait()
        return "run-only"

    # Start two concurrent lookups
    t1 = asyncio.create_task(
        cache.lookup_or_remember(
            conversation_id="conv-A",
            idempotency_key="k1",
            run_id_factory_async=slow_factory,
        )
    )
    # Let t1 acquire the lock and start the slow factory
    await asyncio.sleep(0)
    t2 = asyncio.create_task(
        cache.lookup_or_remember(
            conversation_id="conv-A",
            idempotency_key="k1",
            run_id_factory_async=slow_factory,
        )
    )
    # Unblock t1's factory
    await asyncio.sleep(0)
    factory_event.set()

    r1, cached1 = await t1
    r2, cached2 = await t2

    assert r1 == r2 == "run-only"
    # One of the two must observe the cached value; both cannot be
    # ``was_cached=False``.
    assert {cached1, cached2} == {False, True}
    assert len(factory_calls) == 1, (
        "factory invoked twice means two run rows + two NATS "
        "publishes — the lock is broken"
    )


@pytest.mark.asyncio
async def test_terminal_run_inside_window_still_returns_same_run_id() -> None:
    """Per 01b prompt: a re-send during the window returns the cached
    run_id even if the run has since reached terminal state.

    The cache layer doesn't know about run status — the rule is
    "same key + same conversation + within window -> same run_id".
    The client GETs ``/api/v1/runs/{id}`` to see the terminal state.
    This test pins the layering: the cache ignores terminal-ness.
    """
    cache = IdempotencyCache(window_s=60.0)

    async def factory() -> str:
        return "run-1"

    r1, _ = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="k1",
        run_id_factory_async=factory,
    )
    # Whatever the application does with run-1 (mark terminal,
    # etc.) is irrelevant to the cache.
    r2, cached = await cache.lookup_or_remember(
        conversation_id="conv-A",
        idempotency_key="k1",
        run_id_factory_async=factory,
    )
    assert r1 == r2 == "run-1"
    assert cached is True
