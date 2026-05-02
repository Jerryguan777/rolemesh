"""Tests for the safety maintenance loop (approval_context TTL).

We test the DB-level cleanup function directly rather than spinning
the full loop; the loop itself is a trivial periodic wrapper that's
exercised by the orchestrator startup test separately.

Invariants:
  - Only rows with ``verdict_action='require_approval'`` lose their
    approval_context. Other rows (block / allow / warn / redact)
    never had one set in the first place, but the UPDATE WHERE
    clause guards the filter explicitly.
  - Only rows older than the retention window are cleared. A row
    just landed this minute must be left alone.
  - Rows without approval_context are left alone (no spurious writes).
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.db import (
    _get_pool,
    cleanup_old_safety_approval_contexts,
    create_coworker,
    create_tenant,
    get_safety_decision,
    insert_safety_decision,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _fresh_tenant_cw() -> tuple[str, str]:
    tenant = await create_tenant(
        name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
    )
    cw = await create_coworker(
        tenant_id=tenant.id, name="cw",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    return tenant.id, cw.id


async def _insert_with_age(
    *,
    tenant_id: str,
    coworker_id: str,
    verdict_action: str,
    approval_context: dict[str, object] | None,
    age_hours: int,
) -> str:
    """Insert a decision and backdate created_at so we can test the TTL."""
    decision_id = await insert_safety_decision(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        stage="pre_tool_call",
        verdict_action=verdict_action,
        triggered_rule_ids=[],
        findings=[],
        context_digest="",
        context_summary="",
        approval_context=approval_context,
    )
    pool = _get_pool()  # type: ignore[attr-defined]
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE safety_decisions "
            "SET created_at = now() - make_interval(hours => $1) "
            "WHERE id = $2::uuid",
            age_hours,
            decision_id,
        )
    return decision_id


class TestCleanupOldApprovalContexts:
    @pytest.mark.asyncio
    async def test_clears_context_older_than_retention(self) -> None:
        tid, cwid = await _fresh_tenant_cw()
        old_id = await _insert_with_age(
            tenant_id=tid,
            coworker_id=cwid,
            verdict_action="require_approval",
            approval_context={
                "tool_name": "x", "tool_input": {"a": 1},
                "mcp_server_name": "",
            },
            age_hours=48,  # older than retention
        )
        cleared = await cleanup_old_safety_approval_contexts(
            retention_hours=24
        )
        assert cleared == 1
        row = await get_safety_decision(old_id, tenant_id=tid)
        assert row is not None
        assert row["approval_context"] is None

    @pytest.mark.asyncio
    async def test_leaves_recent_rows_alone(self) -> None:
        tid, cwid = await _fresh_tenant_cw()
        recent_id = await _insert_with_age(
            tenant_id=tid,
            coworker_id=cwid,
            verdict_action="require_approval",
            approval_context={
                "tool_name": "x", "tool_input": {}, "mcp_server_name": "",
            },
            age_hours=1,  # well inside retention
        )
        cleared = await cleanup_old_safety_approval_contexts(
            retention_hours=24
        )
        assert cleared == 0
        row = await get_safety_decision(recent_id, tenant_id=tid)
        assert row is not None
        assert row["approval_context"] is not None

    @pytest.mark.asyncio
    async def test_ignores_non_approval_rows(self) -> None:
        # block rows never have approval_context. The WHERE clause
        # should not touch them even if a weird DB state ever gave one.
        tid, cwid = await _fresh_tenant_cw()
        await _insert_with_age(
            tenant_id=tid,
            coworker_id=cwid,
            verdict_action="block",
            approval_context=None,
            age_hours=48,
        )
        cleared = await cleanup_old_safety_approval_contexts(
            retention_hours=24
        )
        assert cleared == 0

    @pytest.mark.asyncio
    async def test_retention_boundary(self) -> None:
        # Exactly at the retention boundary is safe to clear (older
        # than ``retention`` strict-less-than). Just-younger stays.
        tid, cwid = await _fresh_tenant_cw()
        a_young = await _insert_with_age(
            tenant_id=tid,
            coworker_id=cwid,
            verdict_action="require_approval",
            approval_context={"tool_name": "x", "tool_input": {}},
            age_hours=23,
        )
        a_old = await _insert_with_age(
            tenant_id=tid,
            coworker_id=cwid,
            verdict_action="require_approval",
            approval_context={"tool_name": "y", "tool_input": {}},
            age_hours=25,
        )
        cleared = await cleanup_old_safety_approval_contexts(
            retention_hours=24
        )
        assert cleared == 1
        young_row = await get_safety_decision(a_young, tenant_id=tid)
        old_row = await get_safety_decision(a_old, tenant_id=tid)
        assert young_row is not None
        assert old_row is not None
        assert young_row["approval_context"] is not None
        assert old_row["approval_context"] is None
