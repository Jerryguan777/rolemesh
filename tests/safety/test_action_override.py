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

from typing import Any

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
        # from the underlying check so audit readers see WHY the turn
        # was gated.
        assert verdict.action == "require_approval"
        assert verdict.reason == "matched"
        assert verdict.findings and verdict.findings[0].code == "X"
        # Audit event carries the overridden action so downstream
        # readers see the final verdict.
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
        # MUST NOT turn it into a block — otherwise every
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
