"""Tests for SafetyEngine.run_orchestrator_pipeline.

The orchestrator-side pipeline (introduced in V2 P0.1) runs the shared
pipeline for stages the server produces itself — currently only
MODEL_OUTPUT. Unlike the container path it:

  - bypasses NATS (publishes directly to the audit sink),
  - awaits the audit write, so a test can assert synchronous ordering,
  - still respects the zero-rule zero-overhead invariant.

These tests pin the behavioural contract so the MODEL_OUTPUT call site
in ``rolemesh.main`` can trust it. The container-side pipeline invariants
(priority ordering, fail-modes, short-circuit on block) are already
covered by test_pipeline.py and are NOT re-tested here — they share the
same pipeline_core code path, so duplication would be test-mirror
without added signal.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.safety.engine import SafetyEngine
from rolemesh.safety.registry import (
    CheckRegistry,
    build_orchestrator_registry,
    reset_orchestrator_registry,
)
from rolemesh.safety.types import (
    CostClass,
    SafetyContext,
    Stage,
    Verdict,
)

from .conftest import make_rule

if TYPE_CHECKING:
    from rolemesh.safety.audit import AuditEvent


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.write_call_count: int = 0
        self.should_raise: Exception | None = None

    async def write(self, event: AuditEvent) -> None:
        self.write_call_count += 1
        if self.should_raise is not None:
            raise self.should_raise
        self.events.append(event)


def _model_ctx(
    tenant_id: str = "tenant-m",
    coworker_id: str = "cw-m",
    text: str = "hello world",
) -> SafetyContext:
    return SafetyContext(
        stage=Stage.MODEL_OUTPUT,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        user_id="user-m",
        job_id="",
        conversation_id="conv-m",
        payload={"text": text},
    )


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    # Some tests mutate the process-wide orchestrator registry via a
    # custom builder. Reset around each test so bleed-through doesn't
    # silently shape a later test's view of registered checks.
    reset_orchestrator_registry()
    yield
    reset_orchestrator_registry()


class TestZeroRuleShortCircuit:
    """Zero-rule zero-overhead is a cross-module contract (see
    maybe_register_safety_handler + this method). Regression here
    means every agent turn in production pays one unnecessary DB
    round-trip and one unnecessary registry read.
    """

    @pytest.mark.asyncio
    async def test_empty_rules_returns_allow_without_touching_sink(
        self,
    ) -> None:
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)
        verdict = await engine.run_orchestrator_pipeline(_model_ctx(), [])
        assert verdict.action == "allow"
        assert sink.write_call_count == 0


class TestDirectSinkPersistence:
    """The orchestrator publisher writes directly to the sink and awaits
    the write. Without the await, a DbAuditSink failure during MODEL_OUTPUT
    audit would stay hidden until the next loop iteration — we assert
    synchronous ordering so that failure path surfaces in the same turn.
    """

    @pytest.mark.asyncio
    async def test_block_writes_audit_event_synchronously(self) -> None:
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)

        # Use pii.regex which is already registered in the default
        # orchestrator registry and advertises MODEL_OUTPUT as a stage.
        rule = make_rule(
            rule_id="r-model",
            stage=Stage.MODEL_OUTPUT,
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        verdict = await engine.run_orchestrator_pipeline(
            _model_ctx(text="leaked 123-45-6789"), [rule]
        )
        assert verdict.action == "block"
        # The sink.write was awaited before pipeline_run returned.
        assert sink.write_call_count == 1
        assert len(sink.events) == 1
        audit = sink.events[0]
        assert audit.stage == "model_output"
        assert audit.verdict_action == "block"
        assert audit.triggered_rule_ids == ["r-model"]
        assert audit.tenant_id == "tenant-m"
        assert audit.coworker_id == "cw-m"
        # Fresh finding was converted from Verdict.findings, not a
        # mix-up from an earlier call.
        assert audit.findings and audit.findings[0]["code"] == "PII.SSN"

    @pytest.mark.asyncio
    async def test_allow_fires_one_audit_per_rule_and_not_the_final_allow(
        self,
    ) -> None:
        """Pipeline publishes audit on each per-rule verdict (both
        allow and block) but NOT on the final no-match allow at the
        tail. This keeps the decisions table clean when no rules
        actually fired — only rules that materially participated.
        """
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)
        # A single pii.regex rule that matches nothing — so verdict is
        # the final tail allow with no per-rule publish.
        rule = make_rule(
            rule_id="r-none",
            stage=Stage.MODEL_OUTPUT,
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        verdict = await engine.run_orchestrator_pipeline(
            _model_ctx(text="totally clean text"), [rule]
        )
        # pii.regex returns allow when nothing matches. Pipeline fires
        # one publish for that per-rule allow. It does NOT fire an
        # extra tail-allow publish. This test guards against a refactor
        # that introduces either double-audit or zero-audit noise.
        assert verdict.action == "allow"
        assert sink.write_call_count == 1

    @pytest.mark.asyncio
    async def test_sink_failure_does_not_propagate(self) -> None:
        """An orchestrator audit-sink write failure must not surface
        as a MODEL_OUTPUT pipeline exception — that would silently
        replace every reply with the fail-close string whenever the
        DB is down.
        """
        sink = _CaptureSink()
        sink.should_raise = RuntimeError("db down")
        engine = SafetyEngine(audit_sink=sink)
        rule = make_rule(
            rule_id="r-fail",
            stage=Stage.MODEL_OUTPUT,
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        # No exception even though sink is broken.
        verdict = await engine.run_orchestrator_pipeline(
            _model_ctx(text="leaked 123-45-6789"), [rule]
        )
        # Block decision still stands regardless of audit failure.
        assert verdict.action == "block"


class TestStagePayloadShape:
    """Checks that the MODEL_OUTPUT payload shape handed to the check
    matches the conventions advertised in SafetyContext docstring.
    """

    @pytest.mark.asyncio
    async def test_payload_contains_text_key(self) -> None:
        captured: list[dict[str, Any]] = []

        class _Capture:
            id = "stub.capture"
            version = "1"
            stages = frozenset({Stage.MODEL_OUTPUT})
            cost_class: CostClass = "cheap"
            supported_codes: frozenset[str] = frozenset()
            config_model = None

            async def check(
                self, ctx: SafetyContext, _config: dict[str, Any]
            ) -> Verdict:
                captured.append(dict(ctx.payload))
                return Verdict(action="allow")

        # Register into a fresh orchestrator registry.
        reset_orchestrator_registry()
        from rolemesh.safety import registry as reg_mod

        # Replace the singleton builder temporarily by seeding
        # _ORCHESTRATOR_REGISTRY ourselves.
        r = CheckRegistry()
        r.register(_Capture())
        reg_mod._ORCHESTRATOR_REGISTRY = r  # type: ignore[attr-defined]

        try:
            sink = _CaptureSink()
            engine = SafetyEngine(audit_sink=sink)
            rule = make_rule(
                rule_id="r-cap",
                stage=Stage.MODEL_OUTPUT,
                check_id="stub.capture",
                config={},
            )
            await engine.run_orchestrator_pipeline(
                _model_ctx(text="the payload body"), [rule]
            )
            assert captured == [{"text": "the payload body"}]
        finally:
            reset_orchestrator_registry()


class TestAsyncPublisherAwaited:
    """Regression guard: if pipeline_core stopped awaiting an awaitable
    publisher return, the orchestrator audit would queue up as an
    un-awaited coroutine and silently no-op. A single call must see
    the sink write-count incremented synchronously.
    """

    @pytest.mark.asyncio
    async def test_single_block_produces_single_sink_write(self) -> None:
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)
        rule = make_rule(
            rule_id="r-x",
            stage=Stage.MODEL_OUTPUT,
            check_id="pii.regex",
            config={"patterns": {"CREDIT_CARD": True}},
        )
        verdict = await engine.run_orchestrator_pipeline(
            _model_ctx(text="card 4111 1111 1111 1111"),
            [rule],
        )
        assert verdict.action == "block"
        # Synchronously present immediately after pipeline_run returned.
        assert sink.write_call_count == 1
        # One more event loop tick must NOT uncover a second write
        # from a buffered coroutine.
        await asyncio.sleep(0)
        assert sink.write_call_count == 1


class TestRegistryUsage:
    @pytest.mark.asyncio
    async def test_uses_orchestrator_registry_not_container(self) -> None:
        """Orchestrator pipeline MUST pick up whatever
        build_orchestrator_registry publishes (V2 slow checks will
        diverge from container registry). P0.1 contains no such
        divergence yet, but the dispatch must already go through
        get_orchestrator_registry so the P1.2+ extensions drop in.
        """
        # Default orchestrator registry contains pii.regex; if
        # somehow the method pointed at an empty registry, the rule
        # would hit "unknown check_id" and allow instead of block.
        sink = _CaptureSink()
        engine = SafetyEngine(audit_sink=sink)
        rule = make_rule(
            rule_id="r-reg",
            stage=Stage.MODEL_OUTPUT,
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        verdict = await engine.run_orchestrator_pipeline(
            _model_ctx(text="SSN 123-45-6789"), [rule]
        )
        assert verdict.action == "block"
        # Sanity: the default orchestrator registry has pii.regex.
        assert "pii.regex" in build_orchestrator_registry().ids()
