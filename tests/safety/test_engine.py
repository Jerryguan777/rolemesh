"""Tests for SafetyEngine — the orchestrator-side façade.

Focus on event-handling path (persisting audit events) and rule load
behaviour (scoped filtering + tenant isolation). Uses a fake AuditSink
to keep engine tests hermetic; DB fidelity is covered by test_db.py.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.db import (
    create_coworker,
    create_safety_rule,
    create_tenant,
    list_safety_decisions,
)
from rolemesh.safety.engine import SafetyEngine

if TYPE_CHECKING:
    from rolemesh.safety.audit import AuditEvent

pytestmark = pytest.mark.usefixtures("test_db")


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.should_raise: Exception | None = None

    async def write(self, event: AuditEvent) -> None:
        if self.should_raise is not None:
            raise self.should_raise
        self.events.append(event)


class TestLoadRules:
    @pytest.mark.asyncio
    async def test_returns_snapshot_dicts(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await create_safety_rule(
            tenant_id=tenant.id, stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        engine = SafetyEngine(audit_sink=_CaptureSink())
        rules = await engine.load_rules_for_coworker(tenant.id, cw.id)
        assert rules and rules[0]["check_id"] == "pii.regex"
        # snapshot dicts must be JSON-serializable (used as
        # AgentInitData wire format).
        assert isinstance(rules[0]["stage"], str)
        assert rules[0]["stage"] == "pre_tool_call"

    @pytest.mark.asyncio
    async def test_excludes_disabled_rules(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await create_safety_rule(
            tenant_id=tenant.id, stage="pre_tool_call",
            check_id="pii.regex", config={}, enabled=False,
        )
        engine = SafetyEngine(audit_sink=_CaptureSink())
        assert await engine.load_rules_for_coworker(tenant.id, cw.id) == []


class TestHandleSafetyEvent:
    @pytest.mark.asyncio
    async def test_persists_well_formed_event(self) -> None:
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)
        payload: dict[str, Any] = {
            "tenant_id": str(uuid.uuid4()),
            "coworker_id": str(uuid.uuid4()),
            "conversation_id": "conv-1",
            "job_id": "job-1",
            "stage": "pre_tool_call",
            "verdict_action": "block",
            "triggered_rule_ids": [str(uuid.uuid4())],
            "findings": [
                {"code": "PII.SSN", "severity": "high",
                 "message": "x", "metadata": {}},
            ],
            "context_digest": "a" * 64,
            "context_summary": "tool=x",
        }
        await engine.handle_safety_event(payload)
        assert len(sink.events) == 1
        ev = sink.events[0]
        assert ev.verdict_action == "block"
        assert ev.stage == "pre_tool_call"
        assert ev.context_digest == "a" * 64

    @pytest.mark.asyncio
    async def test_malformed_event_dropped_silently(self) -> None:
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)
        # Missing required key: must not raise, must not persist.
        await engine.handle_safety_event({"stage": "pre_tool_call"})
        assert sink.events == []

    @pytest.mark.asyncio
    async def test_sink_failure_does_not_propagate(self) -> None:
        # A failing audit sink (DB down, network blip) MUST NOT raise
        # into the subscriber loop; that would poison all subsequent
        # events for that process lifetime.
        sink = _CaptureSink()
        sink.should_raise = RuntimeError("db down")
        engine = SafetyEngine(audit_sink=sink)
        payload: dict[str, Any] = {
            "tenant_id": str(uuid.uuid4()),
            "stage": "pre_tool_call",
            "verdict_action": "block",
            "triggered_rule_ids": [],
            "findings": [],
            "context_digest": "",
            "context_summary": "",
        }
        # No exception raised.
        await engine.handle_safety_event(payload)

    @pytest.mark.asyncio
    async def test_default_sink_persists_to_db(self) -> None:
        # Use the default (DbAuditSink) to confirm the full wiring ends
        # up in safety_decisions.
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        engine = SafetyEngine()  # default DbAuditSink
        payload: dict[str, Any] = {
            "tenant_id": tenant.id,
            "stage": "pre_tool_call",
            "verdict_action": "block",
            "triggered_rule_ids": [],
            "findings": [
                {"code": "PII.SSN", "severity": "high",
                 "message": "m", "metadata": {}}
            ],
            "context_digest": "b" * 64,
            "context_summary": "tool=xyz",
        }
        await engine.handle_safety_event(payload)
        rows = await list_safety_decisions(tenant.id)
        assert len(rows) == 1
        assert rows[0]["stage"] == "pre_tool_call"
        assert rows[0]["findings"][0]["code"] == "PII.SSN"
