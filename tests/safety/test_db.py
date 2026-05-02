"""DB tests for safety_rules + safety_decisions CRUD.

Uses the shared pg_url testcontainer fixture. Exercises:
  - create / get / list / update / delete round-trip
  - tenant isolation (cross-tenant selects return nothing)
  - coworker-scoped vs tenant-wide filtering in list_safety_rules_for_coworker
  - decisions insert + list newest-first
  - delete behaviour: disabled flag vs hard delete

These hit a real Postgres (no mocks) so schema drift shows up
immediately — writing a test DB layer against mocks would mask
column-type mismatches that matter in prod.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.db import (
    create_coworker,
    create_safety_rule,
    create_tenant,
    delete_safety_rule,
    get_safety_rule,
    insert_safety_decision,
    list_safety_decisions,
    list_safety_rules,
    list_safety_rules_for_coworker,
    update_safety_rule,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _tenant_and_coworker() -> tuple[str, str]:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    return t.id, cw.id


class TestSafetyRules:
    @pytest.mark.asyncio
    async def test_create_and_get(self) -> None:
        tid, _ = await _tenant_and_coworker()
        rule = await create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            description="block SSN",
        )
        assert rule.tenant_id == tid
        assert rule.coworker_id is None
        assert rule.stage.value == "pre_tool_call"
        assert rule.check_id == "pii.regex"
        assert rule.config == {"patterns": {"SSN": True}}
        assert rule.enabled is True
        assert rule.description == "block SSN"

        got = await get_safety_rule(rule.id, tenant_id=tid)
        assert got is not None
        assert got.id == rule.id

    @pytest.mark.asyncio
    async def test_coworker_scoped_rule(self) -> None:
        tid, cwid = await _tenant_and_coworker()
        rule = await create_safety_rule(
            tenant_id=tid,
            coworker_id=cwid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"EMAIL": True}},
        )
        assert rule.coworker_id == cwid

    @pytest.mark.asyncio
    async def test_list_filters(self) -> None:
        tid, cwid = await _tenant_and_coworker()
        r1 = await create_safety_rule(
            tenant_id=tid, stage="pre_tool_call",
            check_id="pii.regex", config={}, priority=50,
        )
        _r2 = await create_safety_rule(
            tenant_id=tid, stage="input_prompt",
            check_id="pii.regex", config={}, priority=10,
        )
        r3 = await create_safety_rule(
            tenant_id=tid, coworker_id=cwid, stage="pre_tool_call",
            check_id="pii.regex", config={}, priority=99,
        )
        # stage filter
        rows = await list_safety_rules(tid, stage="pre_tool_call")
        ids = {r.id for r in rows}
        assert r1.id in ids and r3.id in ids
        # Priority descending: r3 (99) before r1 (50)
        assert [r.id for r in rows] == [r3.id, r1.id]

    @pytest.mark.asyncio
    async def test_list_for_coworker_includes_tenant_wide(self) -> None:
        tid, cwid = await _tenant_and_coworker()
        # tenant-wide + coworker-scoped + disabled → only first two surface
        tenant_wide = await create_safety_rule(
            tenant_id=tid, stage="pre_tool_call",
            check_id="pii.regex", config={}, priority=10,
        )
        cw_scoped = await create_safety_rule(
            tenant_id=tid, coworker_id=cwid, stage="pre_tool_call",
            check_id="pii.regex", config={}, priority=50,
        )
        disabled = await create_safety_rule(
            tenant_id=tid, coworker_id=cwid, stage="pre_tool_call",
            check_id="pii.regex", config={}, enabled=False,
        )
        rows = await list_safety_rules_for_coworker(tid, cwid)
        ids = {r.id for r in rows}
        assert tenant_wide.id in ids
        assert cw_scoped.id in ids
        assert disabled.id not in ids

    @pytest.mark.asyncio
    async def test_tenant_isolation(self) -> None:
        # A rule created under tenant A must never appear in tenant B's list.
        tid_a, cw_a = await _tenant_and_coworker()
        tid_b, cw_b = await _tenant_and_coworker()
        await create_safety_rule(
            tenant_id=tid_a, coworker_id=cw_a, stage="pre_tool_call",
            check_id="pii.regex", config={},
        )
        rows_b = await list_safety_rules_for_coworker(tid_b, cw_b)
        assert rows_b == []

    @pytest.mark.asyncio
    async def test_update_partial(self) -> None:
        tid, _ = await _tenant_and_coworker()
        rule = await create_safety_rule(
            tenant_id=tid, stage="pre_tool_call",
            check_id="pii.regex", config={"patterns": {"SSN": True}},
            enabled=True, priority=50,
        )
        updated = await update_safety_rule(
            rule.id, tenant_id=tid, enabled=False, priority=75
        )
        assert updated is not None
        assert updated.enabled is False
        assert updated.priority == 75
        # Untouched fields preserved
        assert updated.config == {"patterns": {"SSN": True}}
        # updated_at moved forward
        assert updated.updated_at >= rule.updated_at

    @pytest.mark.asyncio
    async def test_update_missing_returns_none(self) -> None:
        # Random tenant_id ensures the WHERE id = ... AND tenant_id = ...
        # finds nothing — that's the case under test (UPDATE matches no
        # row), regardless of which axis fails to match.
        assert (
            await update_safety_rule(
                str(uuid.uuid4()),
                tenant_id=str(uuid.uuid4()),
                enabled=False,
            )
        ) is None

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        tid, _ = await _tenant_and_coworker()
        rule = await create_safety_rule(
            tenant_id=tid, stage="pre_tool_call",
            check_id="pii.regex", config={},
        )
        assert await delete_safety_rule(rule.id, tenant_id=tid) is True
        assert await get_safety_rule(rule.id, tenant_id=tid) is None
        assert await delete_safety_rule(rule.id, tenant_id=tid) is False


class TestSafetyDecisions:
    @pytest.mark.asyncio
    async def test_insert_and_list(self) -> None:
        tid, cwid = await _tenant_and_coworker()
        rule = await create_safety_rule(
            tenant_id=tid, stage="pre_tool_call",
            check_id="pii.regex", config={},
        )
        decision_id = await insert_safety_decision(
            tenant_id=tid,
            coworker_id=cwid,
            job_id="job-1",
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[rule.id],
            findings=[
                {
                    "code": "PII.SSN", "severity": "high",
                    "message": "SSN detected", "metadata": {},
                },
            ],
            context_digest="a" * 64,
            context_summary="tool=github__create_issue",
        )
        assert uuid.UUID(decision_id)  # valid UUID
        rows = await list_safety_decisions(tid)
        assert len(rows) == 1
        assert rows[0]["verdict_action"] == "block"
        assert rows[0]["triggered_rule_ids"] == [rule.id]
        assert rows[0]["findings"][0]["code"] == "PII.SSN"
        assert rows[0]["context_digest"] == "a" * 64

    @pytest.mark.asyncio
    async def test_verdict_filter(self) -> None:
        tid, _ = await _tenant_and_coworker()
        await insert_safety_decision(
            tenant_id=tid, stage="pre_tool_call",
            verdict_action="block", triggered_rule_ids=[],
            findings=[], context_digest="x" * 64,
            context_summary="",
        )
        await insert_safety_decision(
            tenant_id=tid, stage="pre_tool_call",
            verdict_action="allow", triggered_rule_ids=[],
            findings=[], context_digest="y" * 64,
            context_summary="",
        )
        blocks = await list_safety_decisions(tid, verdict_action="block")
        assert len(blocks) == 1
        assert blocks[0]["verdict_action"] == "block"

    @pytest.mark.asyncio
    async def test_cross_tenant_isolation(self) -> None:
        tid_a, _ = await _tenant_and_coworker()
        tid_b, _ = await _tenant_and_coworker()
        await insert_safety_decision(
            tenant_id=tid_a, stage="pre_tool_call",
            verdict_action="block", triggered_rule_ids=[],
            findings=[], context_digest="z" * 64, context_summary="",
        )
        assert await list_safety_decisions(tid_b) == []
