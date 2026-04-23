"""End-to-end test for the V1 acceptance scenario (design §5.12).

Exercises the full loop without containers:
  1. Admin POSTs a rule via REST (simulated via direct pg.create).
  2. container_executor loads rule snapshots at job start (simulated:
     we call ``list_safety_rules_for_coworker`` directly).
  3. Snapshot is serialized into AgentInitData and deserialized back
     to mimic the NATS KV → container hop.
  4. SafetyHookHandler runs against a ToolCallEvent containing SSN,
     returns a block verdict.
  5. Audit event captured from ToolContext.publish is fed through the
     orchestrator-side SafetyEventsSubscriber (which performs the
     trusted-tenant lookup) and then to SafetyEngine → safety_decisions.
  6. Disable the rule → next run passes the same SSN (hot-update at
     job boundary).

We feed subscriber.on_message_bytes directly rather than standing up
a real NATS server — the subscriber owns the JSON decode and trust
check logic, so this integration still exercises the authoritative
lookup path. The bytes hop in particular matters: a refactor that
accidentally type-narrowed on_payload away from accepting "bytes-
first from NATS" would surface here as a JSON decode failure.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_container_registry
from rolemesh.db import pg
from rolemesh.ipc.protocol import AgentInitData
from rolemesh.safety.engine import SafetyEngine
from rolemesh.safety.subscriber import (
    SafetyEventsSubscriber,
    TrustedCoworker,
)

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

    def get_tool_reversibility(self, _tool_name: str) -> bool:
        return False


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
            registry=build_container_registry(),
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
        #    safety_decisions. We feed bytes through the real subscriber
        #    so the JSON decode + tenant trust-check path is exercised
        #    end-to-end. Calling engine.handle_safety_event directly
        #    (as the previous test did) skipped the trust boundary —
        #    the most security-relevant step.
        assert tool_ctx.events, "hook handler must publish an audit event"
        _, event_payload = tool_ctx.events[0]

        # Orchestrator lookup of a known coworker: return its trusted
        # identity. Simulates _state.coworkers.get in production.
        @dataclass(frozen=True)
        class _TrustedRec:
            tenant_id: str
            id: str

        def _lookup(claimed_coworker_id: str) -> TrustedCoworker | None:
            if claimed_coworker_id == cw.id:
                return _TrustedRec(tenant_id=tenant.id, id=cw.id)
            return None

        engine = SafetyEngine()
        subscriber = SafetyEventsSubscriber(
            engine=engine, coworker_lookup=_lookup
        )
        await subscriber.on_message_bytes(
            json.dumps(event_payload).encode()
        )

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
    async def test_disabling_rule_lets_next_job_allow_ssn(self) -> None:
        # §5.12 acceptance: toggling enabled=false must take effect on
        # the NEXT job load. Previous version only asserted that the
        # snapshot query returned []; that's a necessary but far from
        # sufficient check — pipeline / hook_handler could still have
        # bugs that made the handler block despite an empty snapshot.
        # This version runs the full container flow twice: once with
        # the rule enabled (asserts block), once with it disabled
        # (asserts no block on the same tool_input).
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

        # Snapshot A: rule enabled, Handler must block.
        snapshot_a = [
            r.to_snapshot_dict()
            for r in await pg.list_safety_rules_for_coworker(
                tenant.id, cw.id
            )
        ]
        handler_a = SafetyHookHandler(
            rules=snapshot_a,
            registry=build_container_registry(),
            tool_ctx=_FakeToolCtx(
                tenant_id=tenant.id, coworker_id=cw.id
            ),  # type: ignore[arg-type]
        )
        v_a = await handler_a.on_pre_tool_use(
            ToolCallEvent(
                tool_name="github__create_issue",
                tool_input={"body": "SSN 123-45-6789"},
            )
        )
        assert v_a is not None and v_a.block, (
            "baseline: enabled rule MUST block SSN"
        )

        # Admin disables the rule.
        await pg.update_safety_rule(rule.id, enabled=False)

        # Snapshot B: disabled rule, fresh Handler, SAME tool_input
        # must now be allowed through. This is the property the
        # previous test advertised but did not verify.
        snapshot_b = [
            r.to_snapshot_dict()
            for r in await pg.list_safety_rules_for_coworker(
                tenant.id, cw.id
            )
        ]
        assert snapshot_b == [], "disabled rule must not surface"
        handler_b = SafetyHookHandler(
            rules=snapshot_b,
            registry=build_container_registry(),
            tool_ctx=_FakeToolCtx(
                tenant_id=tenant.id, coworker_id=cw.id
            ),  # type: ignore[arg-type]
        )
        v_b = await handler_b.on_pre_tool_use(
            ToolCallEvent(
                tool_name="github__create_issue",
                tool_input={"body": "SSN 123-45-6789"},
            )
        )
        assert v_b is None, (
            "after disable + snapshot reload: identical SSN tool_input "
            "MUST pass through — Handler returns None when nothing "
            "blocks"
        )

    @pytest.mark.asyncio
    async def test_zero_rules_path_covered_elsewhere(self) -> None:
        # Kept as a pointer: the "no rules -> no handler" invariant
        # is now exercised by test_fail_mode.py::TestRegistrationGuard
        # which exercises maybe_register_safety_handler directly.
        # Previous placeholder that only asserted list == [] was too
        # shallow — that behaviour is already covered by
        # test_db.py::TestSafetyRules. Leaving this test stub so a
        # reader grep'ing for "zero rules" lands on the new location.
        pass
