"""Shape tests for the core safety types.

These lock the on-wire contract: the string values of Stage and the
frozen-dataclass semantics of SafetyContext / Verdict. A breaking
change to either forces a deliberate version bump in every Check, so
we make accidental drift loud.
"""

from __future__ import annotations

import pytest

from rolemesh.safety.types import (
    CONTROL_STAGES,
    Finding,
    Rule,
    SafetyContext,
    Stage,
    ToolInfo,
    Verdict,
)


class TestStage:
    def test_values_are_stable_strings(self) -> None:
        # Pinned here because they are persisted in DB and sent over NATS.
        assert Stage.INPUT_PROMPT.value == "input_prompt"
        assert Stage.PRE_TOOL_CALL.value == "pre_tool_call"
        assert Stage.POST_TOOL_RESULT.value == "post_tool_result"
        assert Stage.MODEL_OUTPUT.value == "model_output"
        assert Stage.PRE_COMPACTION.value == "pre_compaction"

    def test_control_stages_matches_spec(self) -> None:
        # Observational stages must NOT be in the control set — the
        # pipeline's fail-close vs fail-safe branch keys off this.
        assert Stage.PRE_TOOL_CALL in CONTROL_STAGES
        assert Stage.INPUT_PROMPT in CONTROL_STAGES
        assert Stage.MODEL_OUTPUT in CONTROL_STAGES
        assert Stage.POST_TOOL_RESULT not in CONTROL_STAGES
        assert Stage.PRE_COMPACTION not in CONTROL_STAGES


class TestFinding:
    def test_frozen(self) -> None:
        f = Finding(code="PII.SSN", severity="high", message="x")
        with pytest.raises((AttributeError, Exception)):
            f.code = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        f = Finding(code="X", severity="info", message="m")
        assert f.metadata == {}


class TestVerdict:
    def test_default_is_allow(self) -> None:
        v = Verdict()
        assert v.action == "allow"
        assert v.findings == []
        assert v.modified_payload is None

    def test_frozen(self) -> None:
        v = Verdict(action="block")
        with pytest.raises((AttributeError, Exception)):
            v.action = "allow"  # type: ignore[misc]


class TestSafetyContext:
    def test_frozen(self) -> None:
        ctx = SafetyContext(
            stage=Stage.PRE_TOOL_CALL,
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            job_id="j",
            conversation_id="conv",
            payload={"tool_name": "x"},
        )
        with pytest.raises((AttributeError, Exception)):
            ctx.tenant_id = "other"  # type: ignore[misc]

    def test_tool_defaults_to_none(self) -> None:
        ctx = SafetyContext(
            stage=Stage.INPUT_PROMPT,
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            job_id="j",
            conversation_id="conv",
            payload={"prompt": "hi"},
        )
        assert ctx.tool is None


class TestRule:
    def test_frozen(self) -> None:
        # Rule is frozen so "rule snapshot taken at container start is
        # immutable until the next run" is a type-system guarantee
        # rather than a convention. If a refactor removes frozen=True,
        # this test surfaces it immediately.
        r = Rule(
            id="r1",
            tenant_id="t",
            coworker_id=None,
            stage=Stage.PRE_TOOL_CALL,
            check_id="pii.regex",
            config={},
        )
        with pytest.raises((AttributeError, Exception)):
            r.enabled = False  # type: ignore[misc]

    def test_snapshot_dict_shape(self) -> None:
        r = Rule(
            id="r1",
            tenant_id="t",
            coworker_id=None,
            stage=Stage.PRE_TOOL_CALL,
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            priority=50,
            enabled=True,
            description="test",
        )
        d = r.to_snapshot_dict()
        # Container must see the stage as a string value, not enum —
        # the snapshot crosses JSON and JSON has no enums.
        assert d["stage"] == "pre_tool_call"
        assert d["check_id"] == "pii.regex"
        assert d["config"] == {"patterns": {"SSN": True}}
        assert d["coworker_id"] is None
        assert d["priority"] == 50
        assert d["enabled"] is True


class TestToolInfo:
    def test_reversible_defaults_to_false(self) -> None:
        t = ToolInfo(name="Bash")
        assert t.reversible is False
