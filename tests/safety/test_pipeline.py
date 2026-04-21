"""Pipeline behaviour tests.

Focus: rule filtering + priority ordering + short-circuit on block +
fail-close vs fail-safe semantics + audit publishing. Uses stub checks
rather than pii.regex so test failures point at pipeline bugs and not
detector bugs.

Key invariants pinned here:
  - Disabled rules do not fire.
  - Higher priority runs first (descending).
  - Tenant-wide (coworker_id=None) and coworker-scoped rules both match
    the target coworker; a mismatched coworker_id skips the rule.
  - On block, subsequent rules are skipped and exactly one audit event
    is published for the blocking rule.
  - Control-stage check exception propagates (fail-close).
  - Observational-stage check exception is swallowed (fail-safe).
  - Audit publish raising does not change the verdict (decision > audit).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_runner.safety.pipeline import pipeline_run
from rolemesh.safety.registry import CheckRegistry
from rolemesh.safety.types import (
    CostClass,
    Finding,
    SafetyContext,
    Stage,
    Verdict,
)

from .conftest import CapturePublisher, make_context, make_rule

# ---------------------------------------------------------------------------
# Stub checks — tailored to individual test scenarios
# ---------------------------------------------------------------------------


class _AlwaysAllow:
    id: str = "stub.allow"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(Stage)
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset({"STUB.ALLOW"})

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


class _AlwaysBlock:
    id: str = "stub.block"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(Stage)
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset({"STUB.BLOCKED"})

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        return Verdict(
            action="block",
            reason="stub block",
            findings=[
                Finding(code="STUB.BLOCKED", severity="high", message="x")
            ],
        )


class _AlwaysRaise:
    id: str = "stub.raise"
    version: str = "1"
    stages: frozenset[Stage] = frozenset(Stage)
    cost_class: CostClass = "cheap"
    supported_codes: frozenset[str] = frozenset({"STUB.RAISE"})

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        raise RuntimeError("boom")


class _RecordOrder:
    """Records invocation order so we can assert priority ordering."""

    def __init__(self, name: str, ledger: list[str]) -> None:
        self.id = f"stub.order.{name}"
        self.version = "1"
        self.stages: frozenset[Stage] = frozenset(Stage)
        self.cost_class: CostClass = "cheap"
        self.supported_codes: frozenset[str] = frozenset({"STUB.ORDER"})
        self._ledger = ledger
        self._name = name

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict:
        self._ledger.append(self._name)
        return Verdict(action="allow")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_allow() -> CheckRegistry:
    r = CheckRegistry()
    r.register(_AlwaysAllow())
    return r


@pytest.fixture
def registry_block() -> CheckRegistry:
    r = CheckRegistry()
    r.register(_AlwaysBlock())
    return r


@pytest.fixture
def registry_raise() -> CheckRegistry:
    r = CheckRegistry()
    r.register(_AlwaysRaise())
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRuleFiltering:
    @pytest.mark.asyncio
    async def test_disabled_rule_is_skipped(
        self,
        registry_block: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        rule = make_rule(check_id="stub.block", enabled=False)
        verdict = await pipeline_run(
            [rule], registry_block, make_context(), publisher
        )
        assert verdict.action == "allow"
        assert publisher.events == []

    @pytest.mark.asyncio
    async def test_rule_for_other_stage_skipped(
        self,
        registry_block: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        rule = make_rule(check_id="stub.block", stage=Stage.INPUT_PROMPT)
        ctx = make_context(stage=Stage.PRE_TOOL_CALL)
        verdict = await pipeline_run([rule], registry_block, ctx, publisher)
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_rule_scoped_to_other_coworker_skipped(
        self,
        registry_block: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        rule = make_rule(check_id="stub.block", coworker_id="other-cw")
        ctx = make_context(coworker_id="cw-1")
        verdict = await pipeline_run([rule], registry_block, ctx, publisher)
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_tenant_wide_rule_matches(
        self,
        registry_block: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        # coworker_id=None is tenant-wide; MUST match any coworker.
        rule = make_rule(check_id="stub.block", coworker_id=None)
        verdict = await pipeline_run(
            [rule], registry_block, make_context(), publisher
        )
        assert verdict.action == "block"

    @pytest.mark.asyncio
    async def test_unknown_check_id_skipped(
        self,
        registry_allow: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        rule = make_rule(check_id="does.not.exist")
        verdict = await pipeline_run(
            [rule], registry_allow, make_context(), publisher
        )
        # Unknown check is warned and skipped — the orchestrator may
        # have retired a check while a container is running. This must
        # not fail the turn.
        assert verdict.action == "allow"


class TestPriorityOrdering:
    @pytest.mark.asyncio
    async def test_higher_priority_runs_first(
        self, publisher: CapturePublisher
    ) -> None:
        ledger: list[str] = []
        reg = CheckRegistry()
        reg.register(_RecordOrder("low", ledger))
        reg.register(_RecordOrder("high", ledger))
        rules = [
            make_rule(
                rule_id="r-low", check_id="stub.order.low", priority=10
            ),
            make_rule(
                rule_id="r-high", check_id="stub.order.high", priority=99
            ),
        ]
        await pipeline_run(rules, reg, make_context(), publisher)
        assert ledger == ["high", "low"]


class TestShortCircuit:
    @pytest.mark.asyncio
    async def test_block_short_circuits_downstream_rules(
        self, publisher: CapturePublisher
    ) -> None:
        reg = CheckRegistry()
        reg.register(_AlwaysBlock())
        ledger: list[str] = []
        reg.register(_RecordOrder("after", ledger))
        rules = [
            make_rule(
                rule_id="r-block", check_id="stub.block", priority=99
            ),
            make_rule(
                rule_id="r-after", check_id="stub.order.after", priority=10
            ),
        ]
        verdict = await pipeline_run(rules, reg, make_context(), publisher)
        assert verdict.action == "block"
        # Post-block rule must NOT have executed.
        assert ledger == []

    @pytest.mark.asyncio
    async def test_block_publishes_one_audit_event(
        self, publisher: CapturePublisher
    ) -> None:
        reg = CheckRegistry()
        reg.register(_AlwaysBlock())
        rules = [make_rule(rule_id="r-block", check_id="stub.block")]
        await pipeline_run(rules, reg, make_context(), publisher)
        assert len(publisher.events) == 1
        subject, event = publisher.events[0]
        assert subject.endswith(".safety_events")
        assert event["verdict_action"] == "block"
        assert event["triggered_rule_ids"] == ["r-block"]
        assert event["tenant_id"] == "tenant-1"
        assert event["stage"] == "pre_tool_call"
        assert "context_digest" in event and len(event["context_digest"]) == 64
        assert event["findings"] and event["findings"][0]["code"] == "STUB.BLOCKED"


class TestFailModes:
    @pytest.mark.asyncio
    async def test_control_stage_exception_propagates(
        self,
        registry_raise: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        # Control stage — fail-close; the hook bridge converts this
        # into a block verdict at the SDK boundary.
        rule = make_rule(check_id="stub.raise", stage=Stage.PRE_TOOL_CALL)
        with pytest.raises(RuntimeError, match="boom"):
            await pipeline_run(
                [rule], registry_raise, make_context(stage=Stage.PRE_TOOL_CALL),
                publisher,
            )

    @pytest.mark.asyncio
    async def test_observational_stage_exception_swallowed(
        self,
        registry_raise: CheckRegistry,
        publisher: CapturePublisher,
    ) -> None:
        # POST_TOOL_RESULT is observational — a raising check must not
        # abort the turn; pipeline should skip it and return allow.
        rule = make_rule(
            check_id="stub.raise", stage=Stage.POST_TOOL_RESULT
        )
        verdict = await pipeline_run(
            [rule],
            registry_raise,
            make_context(stage=Stage.POST_TOOL_RESULT),
            publisher,
        )
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_change_verdict(
        self, registry_block: CheckRegistry
    ) -> None:
        # Audit infrastructure is best-effort — if publish raises, the
        # block verdict still stands. A regression where a publish
        # failure flipped to allow would be a critical safety bug.
        pub = CapturePublisher()
        pub.should_raise = RuntimeError("nats down")
        rule = make_rule(check_id="stub.block")
        verdict = await pipeline_run(
            [rule], registry_block, make_context(), pub
        )
        assert verdict.action == "block"
