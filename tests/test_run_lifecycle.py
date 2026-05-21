"""``rolemesh.runs.lifecycle`` integration tests.

The lifecycle helper is the single SQL writer for ``runs``. The bug
bait is the no-resurrection guarantee (a redelivered terminal
event must not overwrite an earlier terminal); both happy-path and
race cases are exercised here against a real Postgres so the
``WHERE status = 'running'`` clause is genuinely validated, not
mocked out.

Mutation cue: if you remove the ``WHERE status = 'running'`` guard
in ``update_run_terminal``, the "second terminal does not overwrite"
test goes red within milliseconds. The other tests exist to keep
the helper's tenant scoping and error envelope honest.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from rolemesh.db import (
    _get_pool,
    create_tenant,
    tenant_conn,
)
from rolemesh.runs.lifecycle import (
    create_run,
    get_run,
    update_run_terminal,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_conversation(tenant_id: str) -> str:
    """Insert a minimal conversation row and return its id.

    The lifecycle helper's only FK requirement is
    ``runs.conversation_id -> conversations.id``; ``coworkers`` and
    ``channel_bindings`` are not part of the path under test, so
    we wire them via the simplest valid graph.
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            tenant_id,
        )
        cw_id = await conn.fetchval(
            "INSERT INTO coworkers (tenant_id, name, folder, agent_backend) "
            "VALUES ($1::uuid, $2, $3, 'claude') RETURNING id::text",
            tenant_id,
            f"cw-{uuid.uuid4().hex[:6]}",
            f"folder-{uuid.uuid4().hex[:6]}",
        )
        binding_id = await conn.fetchval(
            "INSERT INTO channel_bindings "
            "(tenant_id, coworker_id, channel_type, credentials) "
            "VALUES ($1::uuid, $2::uuid, 'web', '{}'::jsonb) "
            "RETURNING id::text",
            tenant_id,
            cw_id,
        )
        conv_id = await conn.fetchval(
            "INSERT INTO conversations "
            "(tenant_id, coworker_id, channel_binding_id, channel_chat_id) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4) "
            "RETURNING id::text",
            tenant_id,
            cw_id,
            binding_id,
            f"chat-{uuid.uuid4().hex[:6]}",
        )
    return conv_id


async def _make_tenant_and_conv() -> tuple[str, str]:
    t = await create_tenant(name="T", slug=f"runs-{uuid.uuid4().hex[:8]}")
    conv_id = await _seed_conversation(t.id)
    return t.id, conv_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_returns_uuid_and_marks_running() -> None:
    tenant_id, conv_id = await _make_tenant_and_conv()
    async with tenant_conn(tenant_id) as conn:
        run_id = await create_run(
            tenant_id=tenant_id, conversation_id=conv_id, conn=conn,
        )
    assert isinstance(run_id, str) and len(run_id) == 36
    async with tenant_conn(tenant_id) as conn:
        snap = await get_run(run_id=run_id, tenant_id=tenant_id, conn=conn)
    assert snap is not None
    assert snap["status"] == "running"
    assert snap["completed_at"] is None
    assert snap["usage"] is None


@pytest.mark.asyncio
async def test_update_run_terminal_writes_completed_with_usage() -> None:
    tenant_id, conv_id = await _make_tenant_and_conv()
    async with tenant_conn(tenant_id) as conn:
        run_id = await create_run(
            tenant_id=tenant_id, conversation_id=conv_id, conn=conn,
        )
        ok = await update_run_terminal(
            run_id=run_id,
            status="completed",
            usage={"input_tokens": 100, "output_tokens": 50},
            conn=conn,
        )
    assert ok is True
    async with tenant_conn(tenant_id) as conn:
        snap = await get_run(run_id=run_id, tenant_id=tenant_id, conn=conn)
    assert snap is not None
    assert snap["status"] == "completed"
    assert snap["completed_at"] is not None
    assert snap["usage"] == {"input_tokens": 100, "output_tokens": 50}


# ---------------------------------------------------------------------------
# No-resurrection (the load-bearing one)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_terminal_does_not_overwrite_first() -> None:
    """The redelivery race: 'completed' wins, 'cancelled' must noop.

    This is the test that pins ``WHERE status = 'running'``. Remove
    the clause from update_run_terminal and this case flips status
    from completed -> cancelled.
    """
    tenant_id, conv_id = await _make_tenant_and_conv()
    async with tenant_conn(tenant_id) as conn:
        run_id = await create_run(
            tenant_id=tenant_id, conversation_id=conv_id, conn=conn,
        )
        first = await update_run_terminal(
            run_id=run_id, status="completed", conn=conn,
        )
        second = await update_run_terminal(
            run_id=run_id, status="cancelled", conn=conn,
        )
    assert first is True
    assert second is False  # noop
    async with tenant_conn(tenant_id) as conn:
        snap = await get_run(run_id=run_id, tenant_id=tenant_id, conn=conn)
    assert snap is not None
    assert snap["status"] == "completed"  # NOT cancelled


@pytest.mark.asyncio
async def test_failed_then_completed_keeps_failed() -> None:
    """Same race the other way around — failure should also stick."""
    tenant_id, conv_id = await _make_tenant_and_conv()
    async with tenant_conn(tenant_id) as conn:
        run_id = await create_run(
            tenant_id=tenant_id, conversation_id=conv_id, conn=conn,
        )
        await update_run_terminal(
            run_id=run_id,
            status="failed",
            error={"code": "TIMEOUT"},
            conn=conn,
        )
        ok2 = await update_run_terminal(
            run_id=run_id, status="completed", conn=conn,
        )
    assert ok2 is False
    async with tenant_conn(tenant_id) as conn:
        snap = await get_run(run_id=run_id, tenant_id=tenant_id, conn=conn)
    assert snap is not None
    assert snap["status"] == "failed"
    assert snap["error"] == {"code": "TIMEOUT"}


@pytest.mark.asyncio
async def test_update_unknown_run_returns_false_not_raise() -> None:
    """An update to a nonexistent run is a noop, not an error.

    01b's terminal paths (WS error / cancel / scheduler) all converge
    on this helper; making them special-case a missing row would
    rot the same way the resurrection guard did. The contract is
    "this run is terminal one way or another"; returning False
    captures that.
    """
    tenant_id, _ = await _make_tenant_and_conv()
    ghost = str(uuid.uuid4())
    async with tenant_conn(tenant_id) as conn:
        ok = await update_run_terminal(
            run_id=ghost, status="cancelled", conn=conn,
        )
    assert ok is False


@pytest.mark.asyncio
async def test_update_run_terminal_rejects_running_status() -> None:
    """The helper refuses to set status='running' — that's create_run's job."""
    tenant_id, conv_id = await _make_tenant_and_conv()
    async with tenant_conn(tenant_id) as conn:
        run_id = await create_run(
            tenant_id=tenant_id, conversation_id=conv_id, conn=conn,
        )
        with pytest.raises(ValueError):
            await update_run_terminal(
                run_id=run_id, status="running", conn=conn,  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Tenant scoping (INV-1 end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_is_tenant_scoped() -> None:
    """A run created in tenant A is invisible to tenant B's get_run.

    Both RLS *and* the explicit predicate matter — a single-layer
    failure (RLS off, predicate dropped) flips this test red.
    """
    tenant_a, conv_a = await _make_tenant_and_conv()
    tenant_b, _ = await _make_tenant_and_conv()
    async with tenant_conn(tenant_a) as conn:
        run_id = await create_run(
            tenant_id=tenant_a, conversation_id=conv_a, conn=conn,
        )
    async with tenant_conn(tenant_b) as conn:
        snap = await get_run(run_id=run_id, tenant_id=tenant_b, conn=conn)
    assert snap is None


# ---------------------------------------------------------------------------
# messages.run_id wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_message_persists_run_id_into_messages() -> None:
    """The store_message path accepts run_id and survives the round-trip."""
    from rolemesh.db.chat import store_message

    tenant_id, conv_id = await _make_tenant_and_conv()
    async with tenant_conn(tenant_id) as conn:
        run_id = await create_run(
            tenant_id=tenant_id, conversation_id=conv_id, conn=conn,
        )
    await store_message(
        tenant_id=tenant_id,
        conversation_id=conv_id,
        msg_id=f"msg-{uuid.uuid4().hex[:8]}",
        sender="agent",
        sender_name="Bot",
        content="hello",
        timestamp="2026-05-20T00:00:00+00:00",
        is_from_me=True,
        is_bot_message=True,
        run_id=run_id,
    )
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT run_id::text AS run_id FROM messages "
            "WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid "
            "ORDER BY timestamp DESC LIMIT 1",
            tenant_id,
            conv_id,
        )
    assert row is not None
    assert row["run_id"] == run_id


@pytest.mark.asyncio
async def test_store_message_with_dangling_run_id_raises_fk_violation() -> None:
    """The FK is real — a stale run_id rejects at the DB, not later."""
    from rolemesh.db.chat import store_message

    tenant_id, conv_id = await _make_tenant_and_conv()
    ghost_run = str(uuid.uuid4())
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await store_message(
            tenant_id=tenant_id,
            conversation_id=conv_id,
            msg_id=f"msg-{uuid.uuid4().hex[:8]}",
            sender="agent",
            sender_name="Bot",
            content="hello",
            timestamp="2026-05-20T00:00:00+00:00",
            run_id=ghost_run,
        )
