"""Pinned test for chore A — orchestrator-side ``web.run.cancel.*``.

The subscriber is the missing half of 01b: WebUI publishes the
event, this consumer stops the container and writes the
``status='cancelled'`` row. Without these tests, the cancel
endpoint is "fake success" — the event lands but nothing happens.

Real NATS + real Postgres testcontainer. ``runtime`` is mocked
because we don't want the test to require a docker daemon; the
contract here is "the subscriber calls ``runtime.stop(name)``
exactly once when a name is present, and proceeds to the
terminator regardless". Mocking the runtime keeps the assertion
on the *call*, not on Docker's behaviour.

Anti-mirror posture: the tests do not import the subscriber's
internal handler. They publish onto NATS and observe the DB +
the mock runtime — the same surface a real orchestrator boot
would interact with.

Skips itself if NATS isn't reachable so the suite stays runnable
on a laptop without docker-compose; CI smoke must run it.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import nats
import pytest
from nats.js.api import StreamConfig

from rolemesh.db import (
    _get_admin_pool,
    create_coworker,
    create_tenant,
    tenant_conn,
)
from rolemesh.orchestration.run_cancel_subscriber import (
    subscribe_run_cancel,
)
from rolemesh.runs import create_run, update_run_terminal
from rolemesh.runs import terminators as terminators_mod

pytestmark = pytest.mark.usefixtures("test_db")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")


# ---------------------------------------------------------------------------
# Mock runtime
# ---------------------------------------------------------------------------


class _MockRuntime:
    """Minimal ContainerRuntime — only ``stop`` is exercised here.

    A bare class instead of unittest.mock.AsyncMock so the test
    asserts on the recorded call list (typed) rather than on
    side-effects of a magic-mock proxy. ``raise_on_stop`` lets a
    single test simulate a docker daemon hiccup without affecting
    siblings.
    """

    def __init__(self, raise_on_stop: bool = False) -> None:
        self.stop_calls: list[str] = []
        self.raise_on_stop = raise_on_stop

    async def stop(self, name: str, timeout: int = 1) -> None:
        self.stop_calls.append(name)
        if self.raise_on_stop:
            raise RuntimeError("simulated docker daemon hiccup")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _nats_available() -> bool:
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=2)
    except Exception:
        return False
    await nc.close()
    return True


async def _seed_running_run() -> tuple[str, str, str]:
    """Seed (tenant, coworker, conversation, run) and return ids.

    Returns ``(tenant_id, conversation_id, run_id)``. The
    subscriber only needs run/conversation/tenant — coworker is a
    schema requirement for the conversations row.
    """
    t = await create_tenant(
        name=f"T-{uuid.uuid4().hex[:6]}",
        slug=f"rcs-{uuid.uuid4().hex[:8]}",
    )
    cw = await create_coworker(
        tenant_id=t.id,
        name="cw",
        folder=f"f-{uuid.uuid4().hex[:8]}",
        agent_backend="claude",
    )
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings (tenant_id, coworker_id, "
            "channel_type) VALUES ($1::uuid, $2::uuid, 'web') "
            "RETURNING id::text",
            t.id, cw.id,
        )
        conv_id = await conn.fetchval(
            "INSERT INTO conversations (tenant_id, coworker_id, "
            "channel_binding_id, channel_chat_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4) "
            "RETURNING id::text",
            t.id, cw.id, binding_id, uuid.uuid4().hex,
        )
    async with tenant_conn(t.id) as conn:
        run_id = await create_run(
            tenant_id=t.id, conversation_id=conv_id, conn=conn
        )
    return t.id, conv_id, run_id


async def _connect_js() -> tuple[Any, Any]:
    """Connect to NATS + JetStream and ensure the ``web-ipc`` stream exists."""
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    try:
        await js.add_stream(
            StreamConfig(name="web-ipc", subjects=["web.>"], max_age=3600.0)
        )
    except Exception:
        await js.update_stream(
            StreamConfig(name="web-ipc", subjects=["web.>"], max_age=3600.0)
        )
    return nc, js


async def _purge_consumer(js: Any) -> None:
    """Tear down consumer + drop retained messages between tests.

    Each test publishes onto ``web-ipc`` (subjects ``web.>``,
    ``max_age=3600``); the stream retains messages for an hour by
    default. Re-creating the durable consumer in the next test
    would replay every prior test's event — duplicating
    ``runtime.stop`` calls and breaking exact-count assertions.

    The fix: delete the consumer AND purge the messages so the
    next test starts from a clean slate. Production never purges
    (we WANT replay across orchestrator restarts) — this purge
    is purely for test hygiene.
    """
    try:
        await js.delete_consumer("web-ipc", "orch-web-run-cancel")
    except Exception:
        pass
    try:
        await js.purge_stream("web-ipc")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_stops_container_and_writes_cancelled() -> None:
    """The contract end-to-end:

    publish ``web.run.cancel.<id>`` → subscriber stops the
    container exactly once → ``terminate_run_via_user_cancel`` runs
    → ``runs.status='cancelled'`` + ``completed_at`` non-NULL.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping")

    tenant_id, conv_id, run_id = await _seed_running_run()
    runtime = _MockRuntime()
    nc, js = await _connect_js()
    await _purge_consumer(js)

    container_name = f"agent-{run_id[:8]}"

    def _fetch(_conv_id: str) -> str | None:
        assert _conv_id == conv_id
        return container_name

    sub = await subscribe_run_cancel(
        js, runtime=runtime, fetch_active_container=_fetch
    )
    try:
        payload = json.dumps(
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "conversation_id": conv_id,
            }
        ).encode("utf-8")
        await js.publish(f"web.run.cancel.{run_id}", payload)

        # Wait for the DB UPDATE to land (poll directly with awaits
        # since the subscriber writes asynchronously off the NATS
        # callback thread).
        success = False
        for _ in range(50):
            async with tenant_conn(tenant_id) as conn:
                row = await conn.fetchrow(
                    "SELECT status, completed_at FROM runs "
                    "WHERE id = $1::uuid",
                    run_id,
                )
            if row is not None and row["status"] == "cancelled":
                success = True
                assert row["completed_at"] is not None
                break
            await asyncio.sleep(0.1)
        assert success, "run never reached status='cancelled' after publish"

        # runtime.stop must have been called exactly once with the
        # container name returned by the fetch callback.
        assert runtime.stop_calls == [container_name]
    finally:
        await sub.unsubscribe()
        await _purge_consumer(js)
        await nc.close()


# ---------------------------------------------------------------------------
# Race: run already terminal when event arrives
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_terminal_still_calls_runtime_stop_but_terminator_noops() -> None:
    """Run reached ``completed`` before the cancel event lands.

    The subscriber must still call ``runtime.stop`` — if the row
    is terminal but the container is somehow still up, leaving it
    alive is a ghost. The terminator returns False on the gated
    UPDATE; the subscriber must not raise.

    This pins the design intent that the subscriber's two effects
    (stop + UPDATE) are independent: a terminal row does not skip
    the stop.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping")

    tenant_id, conv_id, run_id = await _seed_running_run()
    # Drive the row terminal *before* the event lands.
    async with tenant_conn(tenant_id) as conn:
        assert await update_run_terminal(
            run_id=run_id, status="completed", conn=conn
        )

    runtime = _MockRuntime()
    nc, js = await _connect_js()
    await _purge_consumer(js)

    container_name = f"agent-{run_id[:8]}"

    def _fetch(_conv_id: str) -> str | None:
        return container_name

    sub = await subscribe_run_cancel(
        js, runtime=runtime, fetch_active_container=_fetch
    )
    try:
        await js.publish(
            f"web.run.cancel.{run_id}",
            json.dumps(
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conv_id,
                }
            ).encode("utf-8"),
        )

        # Wait for runtime.stop to be called (the visible side-effect).
        for _ in range(50):
            if runtime.stop_calls == [container_name]:
                break
            await asyncio.sleep(0.1)
        assert runtime.stop_calls == [container_name], (
            "runtime.stop must run even when the row is already "
            "terminal — otherwise a ghost container can survive"
        )

        # The status must NOT have flipped away from 'completed'.
        async with tenant_conn(tenant_id) as conn:
            status = await conn.fetchval(
                "SELECT status FROM runs WHERE id = $1::uuid", run_id
            )
        assert status == "completed", (
            f"already-terminal run was flipped to {status!r} — "
            "WHERE status='running' gate is broken"
        )
    finally:
        await sub.unsubscribe()
        await _purge_consumer(js)
        await nc.close()


# ---------------------------------------------------------------------------
# No active container (already exited)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_active_container_still_writes_cancelled() -> None:
    """The container has exited on its own; cancel event still lands.

    The subscriber must NOT call ``runtime.stop`` (would error on
    a missing name) and MUST still call the terminator so the row
    advances. This pins the "fetch returned None" branch.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping")

    tenant_id, conv_id, run_id = await _seed_running_run()
    runtime = _MockRuntime()
    nc, js = await _connect_js()
    await _purge_consumer(js)

    def _fetch(_conv_id: str) -> str | None:
        return None  # container already exited

    sub = await subscribe_run_cancel(
        js, runtime=runtime, fetch_active_container=_fetch
    )
    try:
        await js.publish(
            f"web.run.cancel.{run_id}",
            json.dumps(
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conv_id,
                }
            ).encode("utf-8"),
        )

        success = False
        for _ in range(50):
            async with tenant_conn(tenant_id) as conn:
                status = await conn.fetchval(
                    "SELECT status FROM runs WHERE id = $1::uuid", run_id
                )
            if status == "cancelled":
                success = True
                break
            await asyncio.sleep(0.1)
        assert success, "row never reached cancelled despite event"
        assert runtime.stop_calls == [], (
            "runtime.stop was called with no container name — "
            "that would error in production"
        )
    finally:
        await sub.unsubscribe()
        await _purge_consumer(js)
        await nc.close()


# ---------------------------------------------------------------------------
# runtime.stop failure must not block the UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_stop_failure_still_advances_state_machine() -> None:
    """Docker hiccup raises from ``runtime.stop`` — UPDATE still runs.

    Without this guarantee, a transient docker daemon error
    would wedge the SPA in a "cancelling…" state forever. The
    subscriber logs the failure but proceeds to the terminator.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping")

    tenant_id, conv_id, run_id = await _seed_running_run()
    runtime = _MockRuntime(raise_on_stop=True)
    nc, js = await _connect_js()
    await _purge_consumer(js)

    container_name = f"agent-{run_id[:8]}"

    def _fetch(_conv_id: str) -> str | None:
        return container_name

    sub = await subscribe_run_cancel(
        js, runtime=runtime, fetch_active_container=_fetch
    )
    try:
        await js.publish(
            f"web.run.cancel.{run_id}",
            json.dumps(
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conv_id,
                }
            ).encode("utf-8"),
        )

        success = False
        for _ in range(50):
            async with tenant_conn(tenant_id) as conn:
                status = await conn.fetchval(
                    "SELECT status FROM runs WHERE id = $1::uuid", run_id
                )
            if status == "cancelled":
                success = True
                break
            await asyncio.sleep(0.1)
        assert success, (
            "runtime.stop failure must not block the terminator UPDATE — "
            "row stayed running which means the SPA is stuck"
        )
        assert runtime.stop_calls == [container_name], (
            "stop must have been attempted exactly once"
        )
    finally:
        await sub.unsubscribe()
        await _purge_consumer(js)
        await nc.close()


# ---------------------------------------------------------------------------
# Mutation guarantee — disabling terminate_run_via_user_cancel
# leaves the row running, proving the wrapper is the load-bearing path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_skip_terminator_leaves_row_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch the terminator to no-op and observe the row
    stays ``running``.

    Per chore A prompt: "把 terminate_run_via_user_cancel 注释掉,
    runs.status 不变 → 红". The mutation is the only way to prove
    INV-6's "every path writes" is actually load-bearing for path
    3 specifically. Without this test, removing the terminator
    call from the subscriber would silently regress.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping")

    tenant_id, conv_id, run_id = await _seed_running_run()
    runtime = _MockRuntime()
    nc, js = await _connect_js()
    await _purge_consumer(js)

    # No-op the wrapper at the import the subscriber uses. Patch
    # both the source module AND the binding the subscriber
    # imported by name so the call site sees the mutation.
    async def _noop(*, run_id: str, conn: Any) -> bool:
        return False

    monkeypatch.setattr(terminators_mod, "terminate_run_via_user_cancel", _noop)
    from rolemesh.orchestration import run_cancel_subscriber as sub_mod
    monkeypatch.setattr(
        sub_mod, "terminate_run_via_user_cancel", _noop
    )

    container_name = f"agent-{run_id[:8]}"

    def _fetch(_conv_id: str) -> str | None:
        return container_name

    sub = await subscribe_run_cancel(
        js, runtime=runtime, fetch_active_container=_fetch
    )
    try:
        await js.publish(
            f"web.run.cancel.{run_id}",
            json.dumps(
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conv_id,
                }
            ).encode("utf-8"),
        )

        # Wait long enough that the subscriber would have run.
        # We can't poll for a state change (the mutation prevents
        # one), so wait for the side-effect that DOES happen
        # (runtime.stop) and then assert the row stayed ``running``.
        for _ in range(50):
            if runtime.stop_calls:
                break
            await asyncio.sleep(0.1)
        assert runtime.stop_calls == [container_name], (
            "subscriber didn't process the event in time; "
            "test cannot conclude about the mutation"
        )

        # Give the no-op terminator a moment to "complete" so we
        # don't race the polling loop above.
        await asyncio.sleep(0.2)

        async with tenant_conn(tenant_id) as conn:
            status = await conn.fetchval(
                "SELECT status FROM runs WHERE id = $1::uuid", run_id
            )
        assert status == "running", (
            f"row reached {status!r} despite terminator being a no-op — "
            "the subscriber is writing via a path that bypasses "
            "terminate_run_via_user_cancel"
        )
    finally:
        await sub.unsubscribe()
        await _purge_consumer(js)
        await nc.close()


# ---------------------------------------------------------------------------
# Malformed payload — ack-and-drop, no DB / runtime side-effects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_payload_acked_without_side_effects() -> None:
    """A garbled JSON event must not crash the subscriber.

    Production NATS replays / network glitches can occasionally
    surface a corrupt frame. The subscriber must ack-and-drop
    rather than infinite-retry.
    """
    if not await _nats_available():
        pytest.skip(f"NATS not reachable at {NATS_URL}; skipping")

    tenant_id, conv_id, run_id = await _seed_running_run()
    runtime = _MockRuntime()
    nc, js = await _connect_js()
    await _purge_consumer(js)

    fetch_called: list[str] = []

    def _fetch(_conv_id: str) -> str | None:
        fetch_called.append(_conv_id)
        return None

    sub = await subscribe_run_cancel(
        js, runtime=runtime, fetch_active_container=_fetch
    )
    try:
        # Not JSON
        await js.publish("web.run.cancel.bogus", b"not-json")
        # JSON but missing fields
        await js.publish(
            "web.run.cancel.bogus2",
            json.dumps({"only": "garbage"}).encode("utf-8"),
        )
        # Give it time to deliver + ack
        await asyncio.sleep(1.5)

        assert runtime.stop_calls == []
        assert fetch_called == [], (
            "fetch_active_container should not run for malformed "
            "events — payload validation must short-circuit first"
        )

        # The running run we seeded must remain untouched.
        async with tenant_conn(tenant_id) as conn:
            status = await conn.fetchval(
                "SELECT status FROM runs WHERE id = $1::uuid", run_id
            )
        assert status == "running"
        _ = conv_id  # silence ruff — kept for parity with siblings
    finally:
        await sub.unsubscribe()
        await _purge_consumer(js)
        await nc.close()
