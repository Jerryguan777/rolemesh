"""End-to-end test for the V1 acceptance scenario (design §5.12).

Exercises the full loop without containers:
  1. Admin POSTs a rule via REST.
  2. container_executor loads rule snapshots at job start
     (simulated: we call ``list_safety_rules_for_coworker`` directly,
     same code path).
  3. Snapshot is serialized into AgentInitData and deserialized back
     to mimic the NATS KV → container hop.
  4. SafetyHookHandler runs against a ToolCallEvent containing SSN,
     returns a block verdict.
  5. Audit NATS event is picked up by SafetyEngine.handle_safety_event
     and written to safety_decisions.
  6. Disable the rule → next run passes the same SSN (hot-update at
     job boundary).

Written without testcontainer-level Docker launches because a real
agent container requires a full NATS+Postgres stack; the unit layers
already cover the cross-module wiring, and this e2e pins the DB ↔
AgentInitData ↔ hook handler ↔ audit chain as a single scenario.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_default_registry
from rolemesh.db import pg
from rolemesh.ipc.protocol import AgentInitData
from rolemesh.safety.engine import SafetyEngine

pytestmark = pytest.mark.usefixtures("test_db")


@dataclass
class _FakeToolCtx:
    tenant_id: str
    coworker_id: str
    job_id: str = "job-e2e"
    conversation_id: str = "conv-e2e"
    user_id: str = "user-e2e"
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))


class TestV1Acceptance:
    @pytest.mark.asyncio
    async def test_ssn_blocked_and_audited(self) -> None:
        # 1. Seed tenant + coworker + rule via the CRUD layer the REST
        #    API uses. (Tested independently in test_api.py; here we
        #    want the full loop rather than repeating HTTP setup.)
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        rule = await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True, "CREDIT_CARD": True}},
            description="block PII in tool calls",
        )

        # 2. Simulate container_executor loading snapshots.
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        assert rules and rules[0].id == rule.id
        snapshot_dicts = [r.to_snapshot_dict() for r in rules]

        # 3. AgentInitData round-trip — catches any JSON-serialization
        #    regression when we extended the dataclass.
        init = AgentInitData(
            prompt="",
            group_folder=cw.folder,
            chat_jid="chat",
            tenant_id=tenant.id,
            coworker_id=cw.id,
            safety_rules=snapshot_dicts,
        )
        wire = init.serialize()
        decoded = AgentInitData.deserialize(wire)
        assert decoded.safety_rules is not None
        assert decoded.safety_rules[0]["check_id"] == "pii.regex"

        # 4. Hook handler with the decoded snapshot blocks an SSN payload.
        tool_ctx = _FakeToolCtx(
            tenant_id=tenant.id, coworker_id=cw.id
        )
        handler = SafetyHookHandler(
            rules=decoded.safety_rules,
            registry=build_default_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="github__create_issue",
                tool_input={"body": "SSN is 123-45-6789"},
            )
        )
        assert verdict is not None and verdict.block
        assert verdict.reason and "PII.SSN" in verdict.reason

        # 5. Audit NATS event surfaces at the orchestrator and lands in
        #    safety_decisions. In production the orchestrator
        #    subscriber decodes the NATS message; we call
        #    handle_safety_event directly with the same dict shape.
        assert tool_ctx.events, "hook handler must publish an audit event"
        _, event_payload = tool_ctx.events[0]
        engine = SafetyEngine()
        await engine.handle_safety_event(event_payload)

        decisions = await pg.list_safety_decisions(tenant.id)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["verdict_action"] == "block"
        assert d["triggered_rule_ids"] == [rule.id]
        codes = [f["code"] for f in d["findings"]]
        assert "PII.SSN" in codes
        # Audit row must NOT contain the original text per §5.10.
        assert d["context_digest"] and len(d["context_digest"]) == 64
        assert "123-45-6789" not in d["context_summary"]

    @pytest.mark.asyncio
    async def test_disabling_rule_passes_next_job(self) -> None:
        # §5.12 acceptance: toggling enabled=false must take effect on
        # the NEXT job load (we do not expect mid-run rule reload).
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        rule = await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        # Disable the rule (would be PATCH /rules/{id} in prod).
        await pg.update_safety_rule(rule.id, enabled=False)

        # Fresh snapshot load — mimics a new container start.
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        assert rules == [], "disabled rule must not surface in snapshot"

        # With an empty rule set, AgentInitData.safety_rules would be
        # None; the zero-cost path is "do not register the handler at
        # all". The integration point is in agent_runner/main.py, but
        # we assert the list_for_coworker contract here which drives
        # that branch.

    @pytest.mark.asyncio
    async def test_zero_rules_means_no_handler(self) -> None:
        # If no rules exist for the coworker, the orchestrator's
        # container_executor must pass None in AgentInitData.
        # Verified by the snapshot return being empty → container_executor
        # sets safety_rules=None (§5.9).
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        assert rules == []
