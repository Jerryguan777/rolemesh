"""V2 P1.1 action_override tests.

A rule's ``config.action_override`` turns a check's natural verdict
into a different action without requiring a new check. The main
intended use: retrofit an existing block-style check (pii.regex,
domain_allowlist, …) into a require_approval gate for a specific
tenant/coworker.

Invariants pinned here:
  - Override only applies when the check's natural verdict is non-allow
    (otherwise every invocation becomes a gate — defeats the purpose
    of the check).
  - ``redact`` override is rejected (a check that didn't produce a
    modified_payload can't be retroactively turned into redact).
  - Any unknown override value is rejected (no silent no-op).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.safety.pipeline_core import pipeline_run
from rolemesh.safety.registry import CheckRegistry
from rolemesh.safety.types import (
    CostClass,
    Finding,
    SafetyContext,
    Stage,
    Verdict,
)

from .conftest import CapturePublisher, make_context, make_rule

if TYPE_CHECKING:
    from rolemesh.safety.audit import AuditEvent


class _AllowCheck:
    id = "stub.allow"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "cheap"
    supported_codes = frozenset({"X"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


class _BlockCheck:
    id = "stub.block"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "cheap"
    supported_codes = frozenset({"X"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(
            action="block",
            reason="matched",
            findings=[Finding(code="X", severity="high", message="m")],
        )


class TestOverrideUpgrade:
    @pytest.mark.asyncio
    async def test_block_verdict_overridden_to_require_approval(
        self, publisher: CapturePublisher
    ) -> None:
        reg = CheckRegistry()
        reg.register(_BlockCheck())
        rule = make_rule(
            check_id="stub.block",
            config={"action_override": "require_approval"},
        )
        verdict = await pipeline_run(
            [rule], reg, make_context(), publisher
        )
        # Override rewrites the action but keeps the reason + findings
        # from the underlying check so audit readers see WHY the
        # approval request exists.
        assert verdict.action == "require_approval"
        assert verdict.reason == "matched"
        assert verdict.findings and verdict.findings[0].code == "X"
        # Audit event carries the overridden action so downstream
        # (SafetyEngine / approval bridge) can dispatch on it.
        assert publisher.events
        _, ev = publisher.events[0]
        assert ev["verdict_action"] == "require_approval"

    @pytest.mark.asyncio
    async def test_block_verdict_overridden_to_warn_does_not_short_circuit(
        self, publisher: CapturePublisher
    ) -> None:
        # warn is non-terminal — a block→warn override must allow
        # later rules to run (design: warn is additive). The block
        # check returns no ``appended_context`` so the post-override
        # warn has no text payload; the final action tails out as
        # allow, but the downstream rule MUST still have executed.
        # This test asserts non-short-circuit rather than the final
        # action, which is the load-bearing invariant here.
        ledger: list[str] = []

        class _Recorder:
            id = "stub.rec"
            version = "1"
            stages = frozenset(Stage)
            cost_class = "cheap"
            supported_codes: frozenset[str] = frozenset()
            config_model = None

            async def check(self, _ctx, _config):  # type: ignore[no-untyped-def]
                ledger.append("recorded")
                return Verdict(action="allow")

        reg = CheckRegistry()
        reg.register(_BlockCheck())
        reg.register(_Recorder())
        rules = [
            make_rule(
                rule_id="r1",
                check_id="stub.block",
                priority=99,
                config={"action_override": "warn"},
            ),
            make_rule(
                rule_id="r2",
                check_id="stub.rec",
                priority=10,
            ),
        ]
        await pipeline_run(rules, reg, make_context(), publisher)
        assert ledger == ["recorded"]
        # Audit event for the block→warn override uses the overridden
        # action string — regression guard for "override must feed
        # downstream dispatch".
        assert publisher.events
        _, ev = publisher.events[0]
        assert ev["verdict_action"] == "warn"


class TestOverrideScope:
    @pytest.mark.asyncio
    async def test_override_does_not_upgrade_allow(
        self, publisher: CapturePublisher
    ) -> None:
        # If the check didn't detect anything (allow), the override
        # MUST NOT turn it into a block/approval — otherwise every
        # invocation is a gate.
        reg = CheckRegistry()
        reg.register(_AllowCheck())
        rule = make_rule(
            check_id="stub.allow",
            config={"action_override": "block"},
        )
        verdict = await pipeline_run(
            [rule], reg, make_context(), publisher
        )
        assert verdict.action == "allow"


class TestOverrideValidation:
    @pytest.mark.asyncio
    async def test_redact_override_is_rejected(
        self, publisher: CapturePublisher
    ) -> None:
        # A check that returns block cannot be overridden to redact —
        # redact requires a ``modified_payload`` that only the check
        # can produce. The pipeline skips the rule and logs ERROR.
        reg = CheckRegistry()
        reg.register(_BlockCheck())
        rule = make_rule(
            check_id="stub.block",
            config={"action_override": "redact"},
        )
        verdict = await pipeline_run(
            [rule], reg, make_context(), publisher
        )
        # Rule was skipped entirely — no audit, no block.
        assert verdict.action == "allow"
        assert publisher.events == []

    @pytest.mark.asyncio
    async def test_unknown_override_value_skips_rule(
        self, publisher: CapturePublisher
    ) -> None:
        reg = CheckRegistry()
        reg.register(_BlockCheck())
        rule = make_rule(
            check_id="stub.block",
            config={"action_override": "teleport"},
        )
        verdict = await pipeline_run(
            [rule], reg, make_context(), publisher
        )
        assert verdict.action == "allow"
        assert publisher.events == []


class TestApprovalContextPayload:
    """When a require_approval verdict fires at PRE_TOOL_CALL, the
    audit event must carry the full tool_input so the orchestrator's
    approval bridge can create the request without re-deriving context.
    """

    @pytest.mark.asyncio
    async def test_approval_context_attached_at_pre_tool_call(
        self, publisher: CapturePublisher
    ) -> None:
        class _ApprovalCheck:
            id = "stub.approve"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset()
            config_model = None

            async def check(self, _ctx, _config):  # type: ignore[no-untyped-def]
                return Verdict(
                    action="require_approval", reason="needs human"
                )

        reg = CheckRegistry()
        reg.register(_ApprovalCheck())
        rule = make_rule(
            rule_id="r-app",
            check_id="stub.approve",
            stage=Stage.PRE_TOOL_CALL,
        )
        ctx = make_context(
            stage=Stage.PRE_TOOL_CALL,
            payload={
                "tool_name": "mcp__github__create_pr",
                "tool_input": {"title": "risky", "body": "x"},
            },
        )
        await pipeline_run([rule], reg, ctx, publisher)
        assert publisher.events
        _, ev = publisher.events[0]
        assert ev["verdict_action"] == "require_approval"
        assert "approval_context" in ev
        ac = ev["approval_context"]
        assert ac["tool_name"] == "mcp__github__create_pr"
        assert ac["tool_input"] == {"title": "risky", "body": "x"}
        # MCP server name derived from the mcp__{server}__{tool} split.
        assert ac["mcp_server_name"] == "github"

    @pytest.mark.asyncio
    async def test_approval_context_not_attached_on_other_stages(
        self, publisher: CapturePublisher
    ) -> None:
        # INPUT_PROMPT / MODEL_OUTPUT don't fit the approval module's
        # {mcp_server, tool_name, params} model. The audit still
        # records verdict_action=require_approval so operators see
        # the event, but the payload shape isn't retained.
        class _ApprovalCheck:
            id = "stub.approve2"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset()
            config_model = None

            async def check(self, _ctx, _config):  # type: ignore[no-untyped-def]
                return Verdict(action="require_approval")

        reg = CheckRegistry()
        reg.register(_ApprovalCheck())
        rule = make_rule(
            check_id="stub.approve2", stage=Stage.INPUT_PROMPT
        )
        ctx = make_context(
            stage=Stage.INPUT_PROMPT, payload={"prompt": "risky"}
        )
        await pipeline_run([rule], reg, ctx, publisher)
        assert publisher.events
        _, ev = publisher.events[0]
        assert ev["verdict_action"] == "require_approval"
        assert "approval_context" not in ev


class TestSafetyEngineApprovalBridge:
    """SafetyEngine.handle_safety_event dispatches require_approval
    events to the approval handler (if wired). These tests cover the
    dispatch contract with a fake handler so we don't need the real
    approval module.
    """

    @pytest.mark.asyncio
    async def test_dispatches_to_handler_on_require_approval(self) -> None:
        from rolemesh.safety.engine import SafetyEngine

        class _NullSink:
            async def write(self, _event: AuditEvent) -> None:
                return None

        class _CaptureHandler:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def create_from_safety(self, **kwargs: Any) -> None:
                self.calls.append(kwargs)

        handler = _CaptureHandler()
        engine = SafetyEngine(
            audit_sink=_NullSink(), approval_handler=handler
        )
        await engine.handle_safety_event(
            {
                "tenant_id": "t-1",
                "coworker_id": "c-1",
                "conversation_id": "conv-1",
                "job_id": "job-1",
                "stage": "pre_tool_call",
                "verdict_action": "require_approval",
                "triggered_rule_ids": ["r-1"],
                "findings": [],
                "context_digest": "",
                "context_summary": "",
                "approval_context": {
                    "tool_name": "mcp__github__create_pr",
                    "tool_input": {"title": "x"},
                    "mcp_server_name": "github",
                },
            }
        )
        assert len(handler.calls) == 1
        call = handler.calls[0]
        assert call["tenant_id"] == "t-1"
        assert call["tool_name"] == "mcp__github__create_pr"
        assert call["tool_input"] == {"title": "x"}
        assert call["mcp_server_name"] == "github"

    @pytest.mark.asyncio
    async def test_no_handler_logs_warning_and_skips(self) -> None:
        from rolemesh.safety.engine import SafetyEngine

        class _NullSink:
            async def write(self, _event: AuditEvent) -> None:
                return None

        # approval_handler stays None — deployment without the module.
        engine = SafetyEngine(audit_sink=_NullSink())
        # No exception — just a warning log.
        await engine.handle_safety_event(
            {
                "tenant_id": "t-1",
                "coworker_id": "c-1",
                "conversation_id": "conv-1",
                "job_id": "job-1",
                "stage": "pre_tool_call",
                "verdict_action": "require_approval",
                "triggered_rule_ids": ["r-1"],
                "findings": [],
                "context_digest": "",
                "context_summary": "",
                "approval_context": {
                    "tool_name": "x",
                    "tool_input": {},
                    "mcp_server_name": "",
                },
            }
        )

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_cascade(self) -> None:
        from rolemesh.safety.engine import SafetyEngine

        class _NullSink:
            async def write(self, _event: AuditEvent) -> None:
                return None

        class _BrokenHandler:
            async def create_from_safety(self, **_kwargs: Any) -> None:
                raise RuntimeError("approval module down")

        engine = SafetyEngine(
            audit_sink=_NullSink(),
            approval_handler=_BrokenHandler(),
        )
        # Must not raise — a broken approval handler must not poison
        # the safety event ingestion loop.
        await engine.handle_safety_event(
            {
                "tenant_id": "t-1",
                "coworker_id": "c-1",
                "conversation_id": None,
                "job_id": "job-1",
                "stage": "pre_tool_call",
                "verdict_action": "require_approval",
                "triggered_rule_ids": ["r"],
                "findings": [],
                "context_digest": "",
                "context_summary": "",
                "approval_context": {
                    "tool_name": "x",
                    "tool_input": {},
                    "mcp_server_name": "",
                },
            }
        )

    @pytest.mark.asyncio
    async def test_non_require_approval_events_do_not_dispatch(
        self,
    ) -> None:
        from rolemesh.safety.engine import SafetyEngine

        class _NullSink:
            async def write(self, _event: AuditEvent) -> None:
                return None

        class _FailIfCalled:
            async def create_from_safety(self, **_kwargs: Any) -> None:
                raise AssertionError("should not be called")

        engine = SafetyEngine(
            audit_sink=_NullSink(),
            approval_handler=_FailIfCalled(),
        )
        # block event: must NOT dispatch to approval.
        await engine.handle_safety_event(
            {
                "tenant_id": "t",
                "coworker_id": "c",
                "conversation_id": None,
                "job_id": "j",
                "stage": "pre_tool_call",
                "verdict_action": "block",
                "triggered_rule_ids": [],
                "findings": [],
                "context_digest": "",
                "context_summary": "",
            }
        )
