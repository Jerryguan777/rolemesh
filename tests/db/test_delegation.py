"""Tests for ``rolemesh.db.delegation`` — frontdesk v1.2 helpers."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from rolemesh.db import (
    cleanup_running_delegations,
    create_channel_binding,
    create_child_conversation,
    create_conversation,
    create_coworker,
    create_tenant,
    find_child_conversation,
    get_or_create_internal_binding,
    insert_delegation,
    update_delegation_terminal,
)
from rolemesh.db._pool import admin_conn

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_parent_conv() -> tuple[str, str, str, str, str]:
    """tenant, from_coworker, target_coworker, parent_binding, parent_conv."""
    t = await create_tenant(name="DelegCorp", slug=f"deleg-{uuid.uuid4().hex[:8]}")
    frontdesk = await create_coworker(
        tenant_id=t.id,
        name="Frontdesk",
        folder=f"frontdesk-{uuid.uuid4().hex[:8]}",
        agent_role="super_agent",
        is_frontdesk=True,
    )
    trading = await create_coworker(
        tenant_id=t.id,
        name="Trading",
        folder=f"trading-{uuid.uuid4().hex[:8]}",
        agent_role="agent",
        routing_description="Order placement and portfolio queries.",
    )
    binding = await create_channel_binding(
        coworker_id=frontdesk.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    parent = await create_conversation(
        tenant_id=t.id,
        coworker_id=frontdesk.id,
        channel_binding_id=binding.id,
        channel_chat_id="parent-chat",
    )
    return t.id, frontdesk.id, trading.id, binding.id, parent.id


# ---------------------------------------------------------------------------
# get_or_create_internal_binding
# ---------------------------------------------------------------------------


async def test_internal_binding_is_idempotent() -> None:
    """Two callers in a row must return the same binding row."""
    tid, _, target_id, _, _ = await _seed_parent_conv()
    b1 = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    b2 = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    assert b1.id == b2.id
    assert b1.channel_type == "internal"
    assert b1.tenant_id == tid


async def test_internal_binding_isolated_per_coworker() -> None:
    """A second coworker in the same tenant gets a distinct binding row."""
    tid, _, target_a, _, _ = await _seed_parent_conv()
    target_b = await create_coworker(
        tenant_id=tid,
        name="Other",
        folder=f"other-{uuid.uuid4().hex[:8]}",
        agent_role="agent",
    )
    ba = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_a)
    bb = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_b.id)
    assert ba.id != bb.id


# ---------------------------------------------------------------------------
# create_child_conversation + find_child_conversation
# ---------------------------------------------------------------------------


async def test_child_conv_is_created_with_requires_trigger_false() -> None:
    """Regression guard: handbook §6 Step 2.4 ★ rule. If a child is
    ever created with ``requires_trigger=TRUE`` the message loop will
    pick it up and break the "child never enters _state" invariant.
    """
    tid, _, target_id, _, parent_id = await _seed_parent_conv()
    binding = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    child = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="sticky",
    )
    assert child.requires_trigger is False
    assert child.parent_conversation_id == parent_id
    assert child.coworker_id == target_id
    assert child.channel_binding_id == binding.id
    assert child.channel_chat_id == f"internal:{parent_id}:{target_id}"


async def test_sticky_child_conv_on_conflict_returns_existing() -> None:
    """Second sticky create with same (parent, target) returns the same row."""
    tid, _, target_id, _, parent_id = await _seed_parent_conv()
    binding = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    c1 = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="sticky",
    )
    c2 = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="sticky",
    )
    assert c1.id == c2.id


async def test_isolated_child_convs_are_distinct() -> None:
    """Each isolated create generates a fresh UUID-suffixed chat_id → new row."""
    tid, _, target_id, _, parent_id = await _seed_parent_conv()
    binding = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    c1 = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="isolated",
    )
    c2 = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="isolated",
    )
    assert c1.id != c2.id
    assert c1.channel_chat_id != c2.channel_chat_id
    # Both must keep the fixed base prefix so SQL audits can group them
    base = f"internal:{parent_id}:{target_id}:"
    assert c1.channel_chat_id.startswith(base)
    assert c2.channel_chat_id.startswith(base)


async def test_find_child_conv_matches_on_exact_chat_id() -> None:
    """Sticky lookup must not pick up an isolated child for the same (parent, target).

    A prior isolated child has a UUID-suffixed chat_id; a later sticky
    lookup uses the unsuffixed chat_id and must miss the isolated row.
    """
    tid, _, target_id, _, parent_id = await _seed_parent_conv()
    binding = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    isolated = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="isolated",
    )

    sticky_chat_id = f"internal:{parent_id}:{target_id}"
    sticky_lookup = await find_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        channel_chat_id=sticky_chat_id,
    )
    assert sticky_lookup is None, (
        "Sticky lookup must NOT match the existing isolated child conv"
    )

    isolated_lookup = await find_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        channel_chat_id=isolated.channel_chat_id,
    )
    assert isolated_lookup is not None
    assert isolated_lookup.id == isolated.id


async def test_find_child_conv_returns_none_when_absent() -> None:
    tid, _, target_id, _, parent_id = await _seed_parent_conv()
    result = await find_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        channel_chat_id=f"internal:{parent_id}:{target_id}",
    )
    assert result is None


# ---------------------------------------------------------------------------
# delegations row lifecycle
# ---------------------------------------------------------------------------


async def _seed_delegation_row() -> tuple[str, str]:
    """Insert a fresh delegations row, return (tenant_id, delegation_id)."""
    tid, from_id, target_id, _, parent_id = await _seed_parent_conv()
    binding = await get_or_create_internal_binding(tenant_id=tid, coworker_id=target_id)
    child = await create_child_conversation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        target_coworker_id=target_id,
        target_internal_binding_id=binding.id,
        user_id=None,
        mode="sticky",
    )
    delegation_id = await insert_delegation(
        tenant_id=tid,
        parent_conversation_id=parent_id,
        child_conversation_id=child.id,
        from_coworker_id=from_id,
        target_coworker_id=target_id,
        user_id=None,
        prompt_sha256="0" * 64,
        context_mode="sticky",
    )
    return tid, delegation_id


async def test_insert_delegation_starts_in_running_status() -> None:
    _tid, did = await _seed_delegation_row()
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT status, ended_at, duration_ms FROM delegations WHERE id = $1::uuid",
            did,
        )
    assert row is not None
    assert row["status"] == "running"
    assert row["ended_at"] is None
    assert row["duration_ms"] is None


async def test_update_terminal_flips_running_row() -> None:
    tid, did = await _seed_delegation_row()
    flipped = await update_delegation_terminal(
        did, tenant_id=tid, status="success", duration_ms=1234,
    )
    assert flipped is True
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT status, duration_ms, error_message, ended_at "
            "FROM delegations WHERE id = $1::uuid",
            did,
        )
    assert row is not None
    assert row["status"] == "success"
    assert row["duration_ms"] == 1234
    assert row["error_message"] is None
    assert row["ended_at"] is not None


async def test_update_terminal_refuses_to_overwrite_terminal_state() -> None:
    """Late event arriving after a row is already terminal must be a no-op.

    Mutation test value: if the SQL ``WHERE id=$1 AND status='running'``
    is weakened to ``WHERE id=$1``, the second update would overwrite
    the first and corrupt audit history. We assert BOTH the bool return
    AND the on-disk row contents — a test that only checked the bool
    would pass under the mutation.
    """
    tid, did = await _seed_delegation_row()

    first = await update_delegation_terminal(
        did, tenant_id=tid, status="timeout", duration_ms=300_000,
        error_message="took too long",
    )
    assert first is True

    second = await update_delegation_terminal(
        did, tenant_id=tid, status="success", duration_ms=42,
    )
    assert second is False, "Second terminal update must report no-op"

    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT status, duration_ms, error_message "
            "FROM delegations WHERE id = $1::uuid",
            did,
        )
    assert row is not None
    assert row["status"] == "timeout"
    assert row["duration_ms"] == 300_000
    assert row["error_message"] == "took too long"


async def test_cleanup_running_delegations_marks_stale_rows() -> None:
    """Stale 'running' rows (prior crash) must be flipped to 'error' at startup."""
    _tid1, did1 = await _seed_delegation_row()
    tid2, did2 = await _seed_delegation_row()
    # Pre-set one of them to a terminal state so we can prove cleanup
    # leaves terminal rows alone.
    await update_delegation_terminal(
        did2, tenant_id=tid2, status="success", duration_ms=10,
    )

    count = await cleanup_running_delegations()
    assert count >= 1

    async with admin_conn() as conn:
        running_left = await conn.fetchval(
            "SELECT COUNT(*) FROM delegations WHERE status = 'running'"
        )
        row1 = await conn.fetchrow(
            "SELECT status, error_message FROM delegations WHERE id = $1::uuid",
            did1,
        )
        row2 = await conn.fetchrow(
            "SELECT status FROM delegations WHERE id = $1::uuid", did2,
        )
    assert running_left == 0
    assert row1 is not None
    assert row1["status"] == "error"
    assert "cleanup" in (row1["error_message"] or "").lower()
    assert row2 is not None
    assert row2["status"] == "success", (
        "cleanup must not touch already-terminal rows"
    )


async def test_concurrent_terminal_updates_only_one_wins() -> None:
    """Race-style guard: two concurrent terminal flips → exactly one wins.

    Not a true concurrency test (it sequentializes through one DB
    connection per ``asyncio.gather`` task), but it does exercise the
    UPDATE conditional and the bool-return contract under interleaved
    calls.
    """
    tid, did = await _seed_delegation_row()
    a, b = await asyncio.gather(
        update_delegation_terminal(
            did, tenant_id=tid, status="success", duration_ms=10,
        ),
        update_delegation_terminal(
            did, tenant_id=tid, status="error", duration_ms=20,
            error_message="lost the race",
        ),
    )
    assert sorted([a, b]) == [False, True]
