"""Tests for the pipeline's cost_class x reversibility runtime guard.

The guard exists because a slow check at PRE_TOOL_CALL on a reversible
tool (Read, Grep, …) exceeds the 100 ms budget without adding real
safety — the tool has no lasting side effect. Skipping with an ERROR
log is the right move: the rule is misconfigured but the agent turn
proceeds normally.

These tests use stub checks so the signal is about the guard itself,
not the detector logic.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from rolemesh.safety.pipeline_core import pipeline_run
from rolemesh.safety.registry import CheckRegistry
from rolemesh.safety.types import (
    CostClass,
    SafetyContext,
    Stage,
    ToolInfo,
    Verdict,
)

from .conftest import CapturePublisher, make_rule


class _SlowBlocker:
    """Would block if allowed to run — used to detect whether the
    guard actually short-circuited the check."""

    id = "stub.slow.blocker"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "slow"
    supported_codes = frozenset({"X"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="block", reason="slow blocker")


class _CheapBlocker:
    id = "stub.cheap.blocker"
    version = "1"
    stages = frozenset(Stage)
    cost_class: CostClass = "cheap"
    supported_codes = frozenset({"X"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="block", reason="cheap blocker")


def _pretool_ctx(*, reversible: bool) -> SafetyContext:
    return SafetyContext(
        stage=Stage.PRE_TOOL_CALL,
        tenant_id="t",
        coworker_id="c",
        user_id="u",
        job_id="j",
        conversation_id="cv",
        payload={"tool_name": "Read", "tool_input": {}},
        tool=ToolInfo(name="Read", reversible=reversible),
    )


def _model_output_ctx() -> SafetyContext:
    return SafetyContext(
        stage=Stage.MODEL_OUTPUT,
        tenant_id="t",
        coworker_id="c",
        user_id="u",
        job_id="j",
        conversation_id="cv",
        payload={"text": "hi"},
    )


class TestGuardTriggers:
    @pytest.mark.asyncio
    async def test_slow_on_reversible_pretool_is_skipped(
        self,
        publisher: CapturePublisher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reg = CheckRegistry()
        reg.register(_SlowBlocker())
        rule = make_rule(
            check_id="stub.slow.blocker", stage=Stage.PRE_TOOL_CALL
        )
        caplog.set_level(logging.ERROR)
        verdict = await pipeline_run(
            [rule], reg, _pretool_ctx(reversible=True), publisher
        )
        # Guard skipped the rule; pipeline tails out as allow with no
        # audit publish.
        assert verdict.action == "allow"
        assert publisher.events == []
        # ERROR log surfaces for operators.
        assert any(
            "slow check on reversible tool" in r.getMessage()
            for r in caplog.records
        )


class TestGuardDoesNotTrigger:
    @pytest.mark.asyncio
    async def test_slow_on_irreversible_pretool_runs(
        self, publisher: CapturePublisher
    ) -> None:
        reg = CheckRegistry()
        reg.register(_SlowBlocker())
        rule = make_rule(
            check_id="stub.slow.blocker", stage=Stage.PRE_TOOL_CALL
        )
        verdict = await pipeline_run(
            [rule], reg, _pretool_ctx(reversible=False), publisher
        )
        # Slow check against irreversible tool is the whole point of
        # the 2000 ms budget path. Must run and return block.
        assert verdict.action == "block"

    @pytest.mark.asyncio
    async def test_cheap_on_reversible_pretool_runs(
        self, publisher: CapturePublisher
    ) -> None:
        # Cheap checks are not budget-constrained — they run on every
        # stage regardless of reversibility.
        reg = CheckRegistry()
        reg.register(_CheapBlocker())
        rule = make_rule(
            check_id="stub.cheap.blocker", stage=Stage.PRE_TOOL_CALL
        )
        verdict = await pipeline_run(
            [rule], reg, _pretool_ctx(reversible=True), publisher
        )
        assert verdict.action == "block"

    @pytest.mark.asyncio
    async def test_slow_on_non_pretool_stage_runs(
        self, publisher: CapturePublisher
    ) -> None:
        # Guard is scoped to PRE_TOOL_CALL only — a slow check on
        # MODEL_OUTPUT is governed by its own 1000ms budget and
        # has no reversibility to check against.
        reg = CheckRegistry()
        reg.register(_SlowBlocker())
        rule = make_rule(
            check_id="stub.slow.blocker", stage=Stage.MODEL_OUTPUT
        )
        verdict = await pipeline_run(
            [rule], reg, _model_output_ctx(), publisher
        )
        assert verdict.action == "block"
