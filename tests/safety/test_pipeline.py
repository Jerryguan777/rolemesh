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

    @pytest.mark.asyncio
    async def test_rule_stage_not_in_check_stages_skipped(
        self, publisher: CapturePublisher
    ) -> None:
        # A check that only advertises PRE_TOOL_CALL. A rule pointing
        # it at INPUT_PROMPT must be skipped rather than invoked on a
        # payload shape it cannot interpret. Defends against DB drift
        # (direct UPDATE) and check upgrades that drop a stage.
        class _PreToolOnly:
            id = "stub.pretool"
            version = "1"
            stages = frozenset({Stage.PRE_TOOL_CALL})
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(
                self, ctx: SafetyContext, config: dict[str, Any]
            ) -> Verdict:
                # Must not be called; if the guard fails we would
                # reach here and block, not allow.
                return Verdict(
                    action="block", reason="should not be reached"
                )

        reg = CheckRegistry()
        reg.register(_PreToolOnly())
        # Rule says INPUT_PROMPT, ctx is INPUT_PROMPT — but check only
        # supports PRE_TOOL_CALL → skip.
        rule = make_rule(check_id="stub.pretool", stage=Stage.INPUT_PROMPT)
        ctx = make_context(
            stage=Stage.INPUT_PROMPT,
            payload={"prompt": "hello"},
            tool_name="",
        )
        verdict = await pipeline_run([rule], reg, ctx, publisher)
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


class TestUnknownAction:
    """V2 P0.2 pipeline accepts allow/block/redact/warn/require_approval.

    Any other value from a check is a programming error — control stages
    fail-close (re-raise), observational stages skip + ERROR log. This
    pins the rejection behaviour for actions outside the allowed set.
    """

    @pytest.mark.asyncio
    async def test_control_stage_unknown_action_raises(
        self, publisher: CapturePublisher
    ) -> None:
        class _Bogus:
            id = "stub.bogus"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                return Verdict(action="teleport")  # type: ignore[arg-type]

        reg = CheckRegistry()
        reg.register(_Bogus())
        rule = make_rule(check_id="stub.bogus")
        with pytest.raises(ValueError, match="unsupported action"):
            await pipeline_run([rule], reg, make_context(), publisher)

    @pytest.mark.asyncio
    async def test_observational_stage_unknown_action_skipped(
        self, publisher: CapturePublisher
    ) -> None:
        class _Bogus:
            id = "stub.bogus2"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                return Verdict(action="yeet")  # type: ignore[arg-type]

        reg = CheckRegistry()
        reg.register(_Bogus())
        rule = make_rule(check_id="stub.bogus2", stage=Stage.POST_TOOL_RESULT)
        verdict = await pipeline_run(
            [rule], reg, make_context(stage=Stage.POST_TOOL_RESULT),
            publisher,
        )
        # Pipeline should skip this rule. No other rules → tail allow.
        assert verdict.action == "allow"

    @pytest.mark.asyncio
    async def test_control_stage_redact_without_modified_payload_raises(
        self, publisher: CapturePublisher
    ) -> None:
        class _BadRedact:
            id = "stub.badredact"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                return Verdict(action="redact", modified_payload=None)

        reg = CheckRegistry()
        reg.register(_BadRedact())
        rule = make_rule(check_id="stub.badredact")
        with pytest.raises(
            ValueError, match="redact with NoneType modified_payload"
        ):
            await pipeline_run([rule], reg, make_context(), publisher)

    @pytest.mark.asyncio
    async def test_control_stage_redact_with_non_dict_payload_raises(
        self, publisher: CapturePublisher
    ) -> None:
        """Review fix P1-3: the non-dict path (e.g. check returns
        ``modified_payload="CLEANED"`` by mistake) used to slip
        through the audit publish and then silently skip the ctx
        swap — producing an audit row that said "redact happened"
        when nothing was actually redacted. Now both None and
        non-dict fail-close on control stages, with no audit row.
        """

        class _BadRedactShape:
            id = "stub.bad.redact.shape"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                # Non-dict modified_payload is a programmer bug.
                return Verdict(
                    action="redact", modified_payload="CLEANED"
                )

        reg = CheckRegistry()
        reg.register(_BadRedactShape())
        rule = make_rule(check_id="stub.bad.redact.shape")
        with pytest.raises(
            ValueError, match="redact with str modified_payload"
        ):
            await pipeline_run([rule], reg, make_context(), publisher)
        # No audit row — the rule never got past the shape check.
        assert publisher.events == []

    @pytest.mark.asyncio
    async def test_observational_stage_redact_non_dict_skipped_no_audit(
        self, publisher: CapturePublisher
    ) -> None:
        """Observational counterpart: same bug, same fix-up. Rule is
        skipped, nothing published — matches the None path's behavior.
        """

        class _BadRedactShape:
            id = "stub.bad.redact.shape2"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                return Verdict(
                    action="redact", modified_payload=["wrong", "type"]
                )

        reg = CheckRegistry()
        reg.register(_BadRedactShape())
        rule = make_rule(
            check_id="stub.bad.redact.shape2",
            stage=Stage.POST_TOOL_RESULT,
        )
        verdict = await pipeline_run(
            [rule], reg,
            make_context(stage=Stage.POST_TOOL_RESULT),
            publisher,
        )
        # Rule skipped → pipeline tails out as allow, no audit.
        assert verdict.action == "allow"
        assert publisher.events == []


class TestRedactChain:
    """V2 P0.2 redact chain — each redact verdict replaces the payload
    the NEXT rule sees. Regression here would either (a) let later
    rules see the original unredacted payload (safety leak in the
    redact layer), or (b) fail to propagate the final modified payload
    back to the caller (redact has no user-visible effect).
    """

    @pytest.mark.asyncio
    async def test_redact_modifies_payload_for_downstream_rules(
        self, publisher: CapturePublisher
    ) -> None:
        class _RedactFirst:
            """Returns redact that strips the ``secret`` key."""

            id = "stub.redact.first"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                new_payload = dict(ctx.payload)
                new_payload.pop("secret", None)
                return Verdict(
                    action="redact",
                    modified_payload=new_payload,
                    findings=[Finding(
                        code="X", severity="low", message="stripped"
                    )],
                )

        class _InspectAfter:
            """Blocks if payload still contains ``secret`` — i.e., the
            chain did not actually replace the ctx."""

            id = "stub.inspect.after"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"Y"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                if "secret" in ctx.payload:
                    return Verdict(
                        action="block",
                        reason="downstream saw unredacted payload",
                    )
                return Verdict(action="allow")

        reg = CheckRegistry()
        reg.register(_RedactFirst())
        reg.register(_InspectAfter())
        rules = [
            make_rule(
                rule_id="r1",
                check_id="stub.redact.first",
                priority=99,
            ),
            make_rule(
                rule_id="r2",
                check_id="stub.inspect.after",
                priority=10,
            ),
        ]
        ctx = make_context(payload={"tool_name": "x", "secret": "s"})
        verdict = await pipeline_run(rules, reg, ctx, publisher)
        # If redact chaining is broken, _InspectAfter would emit block
        # and pipeline returns block. With correct chaining it sees the
        # stripped payload and returns allow → pipeline tails out as
        # "redact" (the accumulated chain state).
        assert verdict.action == "redact"
        assert verdict.modified_payload is not None
        assert "secret" not in verdict.modified_payload

    @pytest.mark.asyncio
    async def test_multiple_redacts_compose(
        self, publisher: CapturePublisher
    ) -> None:
        class _StripKey:
            def __init__(self, key: str) -> None:
                self.id = f"stub.strip.{key}"
                self.version = "1"
                self.stages: frozenset[Stage] = frozenset(Stage)
                self.cost_class: CostClass = "cheap"
                self.supported_codes: frozenset[str] = frozenset({"X"})
                self._key = key

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                new_payload = dict(ctx.payload)
                new_payload.pop(self._key, None)
                return Verdict(
                    action="redact", modified_payload=new_payload
                )

        reg = CheckRegistry()
        reg.register(_StripKey("a"))
        reg.register(_StripKey("b"))
        rules = [
            make_rule(
                rule_id="r1", check_id="stub.strip.a", priority=99
            ),
            make_rule(
                rule_id="r2", check_id="stub.strip.b", priority=50
            ),
        ]
        ctx = make_context(payload={"a": 1, "b": 2, "c": 3})
        verdict = await pipeline_run(rules, reg, ctx, publisher)
        assert verdict.action == "redact"
        assert verdict.modified_payload == {"c": 3}

    @pytest.mark.asyncio
    async def test_block_after_redact_returns_block_not_redact(
        self, publisher: CapturePublisher
    ) -> None:
        """A rule that blocks after a redact chain must short-circuit
        into a block verdict — the caller should NOT act on
        modified_payload when the final action is block.
        """

        class _Redact:
            id = "stub.redact.x"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                return Verdict(
                    action="redact",
                    modified_payload={"text": "REDACTED"},
                )

        class _Block:
            id = "stub.blocker"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"Y"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                return Verdict(action="block", reason="nope")

        reg = CheckRegistry()
        reg.register(_Redact())
        reg.register(_Block())
        rules = [
            make_rule(
                rule_id="r1", check_id="stub.redact.x", priority=99
            ),
            make_rule(rule_id="r2", check_id="stub.blocker", priority=10),
        ]
        verdict = await pipeline_run(
            rules, reg, make_context(), publisher
        )
        assert verdict.action == "block"
        # Block path does not need modified_payload — the hook
        # translator will substitute the verdict.reason directly.


class TestWarnChain:
    """Warn is purely additive: no short-circuit, multiple warns are
    joined with ``\\n\\n``. A regression that turned warn into a
    short-circuit would prevent downstream detectors from running.
    """

    @pytest.mark.asyncio
    async def test_warn_does_not_short_circuit_downstream(
        self, publisher: CapturePublisher
    ) -> None:
        order: list[str] = []

        class _Warn:
            id = "stub.warn"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                order.append("warn")
                return Verdict(
                    action="warn", appended_context="first warning"
                )

        class _Allow:
            id = "stub.allow.after"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"Y"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                order.append("allow")
                return Verdict(action="allow")

        reg = CheckRegistry()
        reg.register(_Warn())
        reg.register(_Allow())
        rules = [
            make_rule(rule_id="r1", check_id="stub.warn", priority=99),
            make_rule(
                rule_id="r2", check_id="stub.allow.after", priority=10
            ),
        ]
        verdict = await pipeline_run(
            rules, reg, make_context(), publisher
        )
        # Both checks ran (warn did not short-circuit).
        assert order == ["warn", "allow"]
        # Final verdict is warn with the context accumulated.
        assert verdict.action == "warn"
        assert verdict.appended_context == "first warning"

    @pytest.mark.asyncio
    async def test_multiple_warns_join_with_double_newline(
        self, publisher: CapturePublisher
    ) -> None:
        class _WarnA:
            id = "stub.warn.a"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                return Verdict(action="warn", appended_context="A")

        class _WarnB:
            id = "stub.warn.b"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"Y"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                return Verdict(action="warn", appended_context="B")

        reg = CheckRegistry()
        reg.register(_WarnA())
        reg.register(_WarnB())
        rules = [
            make_rule(
                rule_id="r1", check_id="stub.warn.a", priority=99
            ),
            make_rule(
                rule_id="r2", check_id="stub.warn.b", priority=50
            ),
        ]
        verdict = await pipeline_run(
            rules, reg, make_context(), publisher
        )
        assert verdict.action == "warn"
        assert verdict.appended_context == "A\n\nB"


class TestRequireApproval:
    """require_approval short-circuits like block but carries the
    distinct action string so orchestrator audit ingestion (P1.1)
    can create an approval request out-of-band.
    """

    @pytest.mark.asyncio
    async def test_require_approval_short_circuits_with_same_action(
        self, publisher: CapturePublisher
    ) -> None:
        ledger: list[str] = []

        class _Approve:
            id = "stub.approve"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                ledger.append("approve")
                return Verdict(
                    action="require_approval", reason="needs human"
                )

        class _NotReached:
            id = "stub.not.reached"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"Y"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                ledger.append("not_reached")
                return Verdict(action="allow")

        reg = CheckRegistry()
        reg.register(_Approve())
        reg.register(_NotReached())
        rules = [
            make_rule(
                rule_id="r1", check_id="stub.approve", priority=99
            ),
            make_rule(
                rule_id="r2", check_id="stub.not.reached", priority=10
            ),
        ]
        verdict = await pipeline_run(
            rules, reg, make_context(), publisher
        )
        # Short-circuited; second rule not invoked.
        assert ledger == ["approve"]
        # Distinct action so P1.1 can dispatch.
        assert verdict.action == "require_approval"
        assert verdict.reason == "needs human"

    @pytest.mark.asyncio
    async def test_audit_event_carries_require_approval_string(
        self, publisher: CapturePublisher
    ) -> None:
        class _Approve:
            id = "stub.approve2"
            version = "1"
            stages = frozenset(Stage)
            cost_class: CostClass = "cheap"
            supported_codes = frozenset({"X"})

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                return Verdict(action="require_approval")

        reg = CheckRegistry()
        reg.register(_Approve())
        rule = make_rule(rule_id="r-appr", check_id="stub.approve2")
        await pipeline_run([rule], reg, make_context(), publisher)
        assert publisher.events
        _, ev = publisher.events[0]
        # Orchestrator-side audit ingestion dispatches on this exact
        # string — a typo or rename here breaks P1.1.
        assert ev["verdict_action"] == "require_approval"
        assert ev["triggered_rule_ids"] == ["r-appr"]


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
