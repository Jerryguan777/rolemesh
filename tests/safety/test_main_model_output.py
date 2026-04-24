"""Unit tests for rolemesh.main._apply_model_output_safety.

The helper exists so the MODEL_OUTPUT policy (what the user sees on
block vs. fail-close vs. rule-load outage) is reachable without the
full ``_on_output`` closure scaffolding. These tests pin the exact
decision shape a later refactor cannot silently downgrade.

The helper returns a ``ModelOutputSafetyResult`` with mutually-exclusive
``text`` (deliver as normal assistant reply) and ``block`` (deliver
through the dedicated safety-block channel); pre-rework the helper
returned a plain string with the block reason substituted inline,
which let blocks leak into the messages table posing as real replies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.main import _apply_model_output_safety
from rolemesh.safety.engine import SafetyEngine
from rolemesh.safety.types import Verdict

if TYPE_CHECKING:
    from rolemesh.safety.audit import AuditEvent


class _NullSink:
    async def write(self, _event: AuditEvent) -> None:
        return None


class _RaisingEngine:
    """Stand-in SafetyEngine for arranging each terminal verdict shape."""

    def __init__(
        self,
        *,
        rules: list[dict[str, Any]] | None = None,
        load_exc: Exception | None = None,
        pipeline_exc: Exception | None = None,
        pipeline_verdict: Verdict | None = None,
    ) -> None:
        self._rules = rules or []
        self._load_exc = load_exc
        self._pipeline_exc = pipeline_exc
        self._pipeline_verdict = pipeline_verdict or Verdict(action="allow")

    async def load_rules_for_coworker(
        self, _tenant_id: str, _coworker_id: str
    ) -> list[dict[str, Any]]:
        if self._load_exc is not None:
            raise self._load_exc
        return self._rules

    async def run_orchestrator_pipeline(
        self, _ctx: Any, _rules: list[dict[str, Any]]
    ) -> Verdict:
        if self._pipeline_exc is not None:
            raise self._pipeline_exc
        return self._pipeline_verdict


async def _fail_if_called(
    _ctx: Any, _rules: list[dict[str, Any]]
) -> Verdict:
    raise AssertionError("run_orchestrator_pipeline must not be called")


class TestZeroCostPaths:
    @pytest.mark.asyncio
    async def test_none_engine_returns_text_unchanged(self) -> None:
        out = await _apply_model_output_safety(
            safety_engine=None,
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="hello",
        )
        assert out.text == "hello"
        assert out.block is None

    @pytest.mark.asyncio
    async def test_empty_text_bypasses_pipeline(self) -> None:
        # Empty text means nothing to scan; the engine (even if real)
        # must not be touched. This is what the caller relies on to
        # avoid feeding "" into pii.regex in production.
        called: list[str] = []

        class _WatchEngine:
            async def load_rules_for_coworker(self, _t: str, _c: str) -> list:
                called.append("load")
                return []

            async def run_orchestrator_pipeline(
                self, _ctx: Any, _rules: list
            ) -> Verdict:
                called.append("run")
                return Verdict(action="allow")

        out = await _apply_model_output_safety(
            safety_engine=_WatchEngine(),  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="",
        )
        assert out.text == ""
        assert out.block is None
        assert called == []

    @pytest.mark.asyncio
    async def test_no_rules_returns_text_unchanged_without_pipeline(
        self,
    ) -> None:
        engine = _RaisingEngine(rules=[])
        engine.run_orchestrator_pipeline = _fail_if_called  # type: ignore[assignment, method-assign]

        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="hi",
        )
        assert out.text == "hi"
        assert out.block is None


class TestRuleLoadFailureFailsOpen:
    @pytest.mark.asyncio
    async def test_load_raises_returns_text_unchanged(self) -> None:
        # Rule load failure = fail-open (keep text). The container-side
        # pipeline is still running; this method is one of several
        # defense layers. Flipping fail-open to fail-close here would
        # mean a DB blip replaces every agent reply — disproportionate.
        engine = _RaisingEngine(load_exc=RuntimeError("db down"))
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="original response",
        )
        assert out.text == "original response"
        assert out.block is None


class TestBlockVerdict:
    @pytest.mark.asyncio
    async def test_block_with_reason_returns_block_not_text(self) -> None:
        """A block verdict must route through the dedicated block path,
        NOT substitute the assistant text. This is the whole point of
        the refactor away from in-text substitution — blocks that
        used to flow through ResultEvent now flow through
        SafetyBlockEvent and never contaminate messages or metrics.
        """
        rule = {
            "id": "r-b",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {"patterns": {"SSN": True}},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule],
            pipeline_verdict=Verdict(
                action="block",
                reason="Blocked: detected PII.SSN",
            ),
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="my ssn is 123-45-6789",
        )
        assert out.text is None
        # Verdict doesn't carry rule_id at pipeline aggregate level; the
        # per-rule audit lives in safety_decisions. UI gets stage from
        # a separate arg in _on_output — so rule_id is None here by
        # design, not oversight.
        assert out.block == ("Blocked: detected PII.SSN", None)

    @pytest.mark.asyncio
    async def test_block_without_reason_uses_generic_placeholder(
        self,
    ) -> None:
        rule = {
            "id": "r-b",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule],
            pipeline_verdict=Verdict(action="block", reason=None),
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="something",
        )
        assert out.text is None
        assert out.block == ("[Response blocked by safety policy]", None)

class TestPipelineExceptionFailsClosed:
    @pytest.mark.asyncio
    async def test_pipeline_exception_returns_block(self) -> None:
        # MODEL_OUTPUT is a control stage; a check implementation bug
        # that raises must fail-closed, delivered through the block
        # channel (not as a silently-substituted string).
        rule = {
            "id": "r-x",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule], pipeline_exc=RuntimeError("check blew up")
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="original",
        )
        assert out.text is None
        assert out.block == ("[Response blocked by safety policy]", None)


class TestAllowPassesThrough:
    @pytest.mark.asyncio
    async def test_allow_verdict_returns_original_text(self) -> None:
        rule = {
            "id": "r-a",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule], pipeline_verdict=Verdict(action="allow")
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="clean response",
        )
        assert out.text == "clean response"
        assert out.block is None


class TestRedactVerdict:
    """MODEL_OUTPUT is one of the few stages where redact takes effect
    — the orchestrator replaces the user-facing reply with the cleaned
    text. Redact is NOT a block, so it flows through the text channel.
    """

    @pytest.mark.asyncio
    async def test_redact_substitutes_modified_text(self) -> None:
        rule = {
            "id": "r-r",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule],
            pipeline_verdict=Verdict(
                action="redact",
                modified_payload={"text": "cleaned reply"},
            ),
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="original with SSN 123-45-6789",
        )
        assert out.text == "cleaned reply"
        assert out.block is None

    @pytest.mark.asyncio
    async def test_redact_without_text_falls_back_to_original(self) -> None:
        """Defensive — a misbehaving check returning redact with a
        non-string text must not crash. Fall back to original text."""
        rule = {
            "id": "r-r",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule],
            pipeline_verdict=Verdict(
                action="redact",
                modified_payload={"not_text": 42},
            ),
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="untouched",
        )
        assert out.text == "untouched"
        assert out.block is None


class TestWarnVerdict:
    @pytest.mark.asyncio
    async def test_warn_returns_original_text(self) -> None:
        # MODEL_OUTPUT has no context-injection surface; warn is pure
        # audit at this stage.
        rule = {
            "id": "r-w",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule],
            pipeline_verdict=Verdict(
                action="warn", appended_context="heads up"
            ),
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="original reply",
        )
        assert out.text == "original reply"
        assert out.block is None


class TestRequireApprovalVerdict:
    @pytest.mark.asyncio
    async def test_require_approval_treated_like_block(self) -> None:
        # P1.1 will handle the actual approval-request creation via
        # the audit ingestion path. From the user's perspective on
        # MODEL_OUTPUT, the reply is suppressed via the block channel
        # with the verdict's reason.
        rule = {
            "id": "r-ap",
            "tenant_id": "t",
            "coworker_id": None,
            "stage": "model_output",
            "check_id": "pii.regex",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
        engine = _RaisingEngine(
            rules=[rule],
            pipeline_verdict=Verdict(
                action="require_approval",
                reason="needs human review",
            ),
        )
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="sensitive answer",
        )
        assert out.text is None
        assert out.block == ("needs human review", None)


class TestWithRealEngineAndPiiRegex:
    """End-to-end using the real SafetyEngine + pii.regex check so a
    refactor that drops MODEL_OUTPUT from pii.regex's stages or breaks
    the orchestrator registry immediately trips a test.
    """

    @pytest.mark.asyncio
    async def test_pii_in_text_produces_block_channel(self) -> None:
        engine = SafetyEngine(audit_sink=_NullSink())

        async def _fake_load(
            _tid: str, _cid: str
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": "r-real",
                    "tenant_id": "t",
                    "coworker_id": None,
                    "stage": "model_output",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "priority": 100,
                    "enabled": True,
                }
            ]

        engine.load_rules_for_coworker = _fake_load  # type: ignore[method-assign]

        out = await _apply_model_output_safety(
            safety_engine=engine,
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="leaked 123-45-6789",
        )
        assert out.text is None
        assert out.block is not None
        reason, rule_id = out.block
        assert "Blocked" in reason
        assert "PII.SSN" in reason
        assert rule_id is None  # not exposed at pipeline-aggregate level

    @pytest.mark.asyncio
    async def test_clean_text_returns_unchanged(self) -> None:
        engine = SafetyEngine(audit_sink=_NullSink())

        async def _fake_load(
            _tid: str, _cid: str
        ) -> list[dict[str, Any]]:
            return [
                {
                    "id": "r-real",
                    "tenant_id": "t",
                    "coworker_id": None,
                    "stage": "model_output",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "priority": 100,
                    "enabled": True,
                }
            ]

        engine.load_rules_for_coworker = _fake_load  # type: ignore[method-assign]

        out = await _apply_model_output_safety(
            safety_engine=engine,
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="just a friendly reply",
        )
        assert out.text == "just a friendly reply"
        assert out.block is None
