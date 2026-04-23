"""Round-trip tests for the NATS RPC codec.

A silent drift in these shapes (e.g. forgetting to serialize
``metadata`` on the wire) would look fine in container unit tests
but break slow-check execution in production. These tests pin the
exact field set that travels both directions — adding a field to
``SafetyContext`` / ``Verdict`` without extending the codec fails
one of these tests first rather than surfacing under load.
"""

from __future__ import annotations

import json

import pytest

from rolemesh.safety.rpc_codec import (
    deserialize_context,
    deserialize_verdict,
    serialize_context,
    serialize_verdict,
)
from rolemesh.safety.types import (
    Finding,
    SafetyContext,
    Stage,
    ToolInfo,
    Verdict,
)


class TestContextRoundTrip:
    def test_minimal_context_roundtrips(self) -> None:
        ctx = SafetyContext(
            stage=Stage.PRE_TOOL_CALL,
            tenant_id="t1",
            coworker_id="c1",
            user_id="u1",
            job_id="j1",
            conversation_id="cv1",
            payload={"tool_name": "x", "tool_input": {}},
        )
        data = serialize_context(ctx)
        # Must be JSON-safe — no enum objects, no Mapping wrappers.
        json.dumps(data)
        got = deserialize_context(data)
        assert got.stage is Stage.PRE_TOOL_CALL
        assert got.tenant_id == "t1"
        assert got.coworker_id == "c1"
        assert got.user_id == "u1"
        assert got.job_id == "j1"
        assert got.conversation_id == "cv1"
        assert dict(got.payload) == {"tool_name": "x", "tool_input": {}}
        assert got.tool is None
        assert dict(got.metadata) == {}

    def test_context_with_tool_roundtrips(self) -> None:
        ctx = SafetyContext(
            stage=Stage.MODEL_OUTPUT,
            tenant_id="t",
            coworker_id="c",
            user_id="u",
            job_id="j",
            conversation_id="cv",
            payload={"text": "hi"},
            tool=ToolInfo(name="github__pr", reversible=True),
            metadata={"trace_id": "abc"},
        )
        got = deserialize_context(serialize_context(ctx))
        assert got.tool is not None
        assert got.tool.name == "github__pr"
        assert got.tool.reversible is True
        assert dict(got.metadata) == {"trace_id": "abc"}

    def test_unknown_stage_string_raises(self) -> None:
        data = serialize_context(
            SafetyContext(
                stage=Stage.PRE_TOOL_CALL,
                tenant_id="t",
                coworker_id="c",
                user_id="u",
                job_id="j",
                conversation_id="cv",
                payload={},
            )
        )
        data["stage"] = "does_not_exist"
        with pytest.raises(ValueError):
            deserialize_context(data)


class TestVerdictRoundTrip:
    def test_block_verdict_with_findings_roundtrips(self) -> None:
        v = Verdict(
            action="block",
            reason="matched SSN",
            findings=[
                Finding(
                    code="PII.SSN",
                    severity="high",
                    message="x",
                    metadata={"offset": 7},
                )
            ],
        )
        data = serialize_verdict(v)
        json.dumps(data)
        got = deserialize_verdict(data)
        assert got.action == "block"
        assert got.reason == "matched SSN"
        assert len(got.findings) == 1
        f = got.findings[0]
        assert f.code == "PII.SSN"
        assert f.severity == "high"
        assert f.metadata == {"offset": 7}

    def test_redact_verdict_preserves_modified_payload(self) -> None:
        v = Verdict(
            action="redact",
            modified_payload={"text": "CLEANED"},
        )
        got = deserialize_verdict(serialize_verdict(v))
        assert got.action == "redact"
        assert got.modified_payload == {"text": "CLEANED"}

    def test_warn_verdict_preserves_appended_context(self) -> None:
        v = Verdict(action="warn", appended_context="watch out")
        got = deserialize_verdict(serialize_verdict(v))
        assert got.action == "warn"
        assert got.appended_context == "watch out"

    def test_malformed_finding_entries_are_dropped(self) -> None:
        # Malformed findings in a wire payload must not crash the
        # codec — skip them so the verdict action + other findings
        # still round-trip.
        data = {
            "action": "block",
            "reason": "x",
            "modified_payload": None,
            "findings": [
                "not a dict",  # dropped
                {"code": "X", "severity": "high", "message": "ok"},
            ],
            "appended_context": None,
        }
        got = deserialize_verdict(data)
        assert got.action == "block"
        assert len(got.findings) == 1
        assert got.findings[0].code == "X"

    def test_empty_verdict_round_trips(self) -> None:
        got = deserialize_verdict({})
        assert got.action == "allow"
        assert got.findings == []
