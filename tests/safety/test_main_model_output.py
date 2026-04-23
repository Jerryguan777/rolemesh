"""Unit tests for rolemesh.main._apply_model_output_safety.

The helper exists so the MODEL_OUTPUT policy (what text replaces the
reply on block vs. fail-close vs. rule-load outage) is reachable
without the full `_on_output` closure scaffolding. These tests pin
the exact substitution strings so a later refactor cannot silently
downgrade the user-facing message for a block verdict.
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
    """Stand-in for a SafetyEngine whose load_rules_for_coworker raises
    (simulating DB unreachable) or whose run_orchestrator_pipeline raises
    (simulating programmer error).
    """

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
        assert out == "hello"

    @pytest.mark.asyncio
    async def test_empty_text_bypasses_pipeline(self) -> None:
        # Empty text means nothing to scan; the engine (even if real)
        # must not be touched. This is also what the caller relies on
        # to avoid feeding "" into pii.regex in production.
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
        assert out == ""
        assert called == []

    @pytest.mark.asyncio
    async def test_no_rules_returns_text_unchanged_without_pipeline(
        self,
    ) -> None:
        engine = _RaisingEngine(rules=[])

        # If the pipeline is called with no rules, run_orchestrator_pipeline
        # would short-circuit. But the helper is expected to short-circuit
        # even earlier (no rules → skip pipeline entirely) to skip the
        # SafetyContext construction cost. Sentinel below detects it.
        engine.run_orchestrator_pipeline = _fail_if_called  # type: ignore[assignment, method-assign]

        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="hi",
        )
        assert out == "hi"


async def _fail_if_called(
    _ctx: Any, _rules: list[dict[str, Any]]
) -> Verdict:
    raise AssertionError("run_orchestrator_pipeline must not be called")


class TestRuleLoadFailureFailsOpen:
    @pytest.mark.asyncio
    async def test_load_raises_returns_text_unchanged(self) -> None:
        # Rule load failure = fail-open (keep text). The container-side
        # pipeline is still running; this method is one of several
        # defense layers. Flipping fail-open to fail-close here would
        # mean a DB blip replaces every agent reply with a safety
        # placeholder — disproportionate to the actual threat.
        engine = _RaisingEngine(load_exc=RuntimeError("db down"))
        out = await _apply_model_output_safety(
            safety_engine=engine,  # type: ignore[arg-type]
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            conversation_id="conv",
            text="original response",
        )
        assert out == "original response"


class TestBlockVerdict:
    @pytest.mark.asyncio
    async def test_block_with_reason_uses_reason_string(self) -> None:
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
                action="block", reason="Blocked: detected PII.SSN"
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
        assert out == "Blocked: detected PII.SSN"

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
        assert out == "[Response blocked by safety policy]"


class TestPipelineExceptionFailsClosed:
    @pytest.mark.asyncio
    async def test_pipeline_exception_replaces_text(self) -> None:
        # MODEL_OUTPUT is a control stage; a bug in a check that raises
        # all the way up through pipeline_run must fail-closed —
        # otherwise a check implementation error would silently let PII
        # through. The placeholder is shown to the user so they know
        # something was filtered (not a silent empty reply).
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
        assert out == "[Response blocked by safety policy]"


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
        assert out == "clean response"


class TestRedactVerdict:
    """MODEL_OUTPUT is one of the few stages where redact can actually
    take effect — the orchestrator replaces the user-facing reply with
    the cleaned text. This pins that contract.
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
        assert out == "cleaned reply"

    @pytest.mark.asyncio
    async def test_redact_without_text_falls_back_to_original(self) -> None:
        """Defensive — a misbehaving check returning redact with a
        non-string text must not crash the reply path. Fall back to
        original text and log."""
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
        assert out == "untouched"


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
        assert out == "original reply"


class TestRequireApprovalVerdict:
    @pytest.mark.asyncio
    async def test_require_approval_treated_like_block(self) -> None:
        # P1.1 will handle the actual approval-request creation via
        # the audit ingestion path. From the user's perspective on
        # MODEL_OUTPUT, the reply is suppressed with the verdict's
        # reason (or the generic placeholder).
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
        assert out == "needs human review"


class TestWithRealEngineAndPiiRegex:
    """End-to-end using the real SafetyEngine + pii.regex check so a
    refactor that drops MODEL_OUTPUT from pii.regex's stages or
    breaks the orchestrator registry immediately trips a test.
    """

    @pytest.mark.asyncio
    async def test_pii_in_text_produces_block_substitution(self) -> None:
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
        assert "Blocked" in out
        assert "PII.SSN" in out

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
        assert out == "just a friendly reply"
