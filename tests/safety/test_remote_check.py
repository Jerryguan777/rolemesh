"""Unit tests for the container-side RemoteCheck proxy.

These tests use a fake NATS client that returns canned reply bytes
(or raises) — the RPC server is exercised in test_rpc_server.py.
Keeping the two sides separately tested means a change to the wire
protocol fails one file or the other with a clear blame target,
rather than a combined integration test that says "something in the
slow-check path broke".

Fail-open posture is the load-bearing contract here: a broken slow
check must not block agent turns, but MUST surface a critical
finding so audits pick up the outage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from agent_runner.safety.remote import (
    DETECT_SUBJECT_TEMPLATE,
    RemoteCheck,
)
from rolemesh.safety.rpc_codec import serialize_verdict
from rolemesh.safety.types import (
    Finding,
    SafetyContext,
    Stage,
    Verdict,
)


@dataclass
class _FakeMsg:
    data: bytes


class _FakeNats:
    def __init__(self) -> None:
        self.last_subject: str | None = None
        self.last_data: bytes | None = None
        self.last_timeout: float | None = None
        self.reply_data: bytes | None = None
        self.raise_on_request: BaseException | None = None

    async def request(
        self, subject: str, data: bytes, *, timeout: float
    ) -> _FakeMsg:
        self.last_subject = subject
        self.last_data = data
        self.last_timeout = timeout
        if self.raise_on_request is not None:
            raise self.raise_on_request
        if self.reply_data is None:
            raise AssertionError("no canned reply configured for this test")
        return _FakeMsg(data=self.reply_data)


def _ctx(job_id: str = "job-r") -> SafetyContext:
    return SafetyContext(
        stage=Stage.INPUT_PROMPT,
        tenant_id="t",
        coworker_id="c",
        user_id="u",
        job_id=job_id,
        conversation_id="cv",
        payload={"prompt": "hello"},
    )


def _check() -> RemoteCheck:
    return RemoteCheck(
        check_id="llm_guard.prompt_injection",
        version="1",
        stages=frozenset({Stage.INPUT_PROMPT}),
        cost_class="slow",
        supported_codes=frozenset({"PROMPT_INJECTION"}),
        nats_client=_FakeNats(),  # placeholder; overridden per test
        default_timeout_ms=500,
    )


class TestSubjectAndPayload:
    @pytest.mark.asyncio
    async def test_builds_subject_from_job_id_and_sends_request(
        self,
    ) -> None:
        nc = _FakeNats()
        check = RemoteCheck(
            check_id="x",
            version="1",
            stages=frozenset({Stage.INPUT_PROMPT}),
            cost_class="slow",
            supported_codes=frozenset(),
            nats_client=nc,
            default_timeout_ms=500,
        )
        nc.reply_data = json.dumps(
            {
                "request_id": "rid",
                "verdict": serialize_verdict(Verdict(action="allow")),
                "error": None,
            }
        ).encode()
        await check.check(_ctx("job-xyz"), {})
        assert nc.last_subject == DETECT_SUBJECT_TEMPLATE.format(
            job_id="job-xyz"
        )
        # The request body carries the exact check id + serialized
        # context — a check author changing their mind about a config
        # key must not break the codec shape here.
        assert nc.last_data is not None
        body = json.loads(nc.last_data)
        assert body["check_id"] == "x"
        assert body["context"]["stage"] == "input_prompt"
        assert body["context"]["tenant_id"] == "t"
        assert body["config"] == {}

    @pytest.mark.asyncio
    async def test_config_timeout_ms_overrides_default(self) -> None:
        nc = _FakeNats()
        check = RemoteCheck(
            check_id="x",
            version="1",
            stages=frozenset({Stage.INPUT_PROMPT}),
            cost_class="slow",
            supported_codes=frozenset(),
            nats_client=nc,
            default_timeout_ms=1500,
        )
        nc.reply_data = json.dumps(
            {
                "request_id": "rid",
                "verdict": serialize_verdict(Verdict(action="allow")),
                "error": None,
            }
        ).encode()
        await check.check(_ctx(), {"timeout_ms": 200})
        # 200ms → 0.2s. Defends against a refactor that swapped units.
        assert nc.last_timeout == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_underscore_config_keys_are_stripped(self) -> None:
        # Pipeline-internal config keys (prefixed with _) must not
        # reach the orchestrator over the wire. Stripping at this
        # seam prevents internal bookkeeping from polluting the RPC
        # and from reaching third-party check config validation.
        nc = _FakeNats()
        check = RemoteCheck(
            check_id="x",
            version="1",
            stages=frozenset({Stage.INPUT_PROMPT}),
            cost_class="slow",
            supported_codes=frozenset(),
            nats_client=nc,
            default_timeout_ms=500,
        )
        nc.reply_data = json.dumps(
            {
                "request_id": "rid",
                "verdict": serialize_verdict(Verdict(action="allow")),
                "error": None,
            }
        ).encode()
        await check.check(_ctx(), {"_rule_id": "r-1", "threshold": 0.9})
        body = json.loads(nc.last_data or b"{}")
        assert body["config"] == {"threshold": 0.9}


class TestReplyParsing:
    @pytest.mark.asyncio
    async def test_ok_reply_returns_decoded_verdict(self) -> None:
        nc = _FakeNats()
        check = _check()
        check._nc = nc  # type: ignore[attr-defined]
        nc.reply_data = json.dumps(
            {
                "request_id": "rid",
                "verdict": serialize_verdict(
                    Verdict(
                        action="block",
                        reason="detected",
                        findings=[
                            Finding(
                                code="PROMPT_INJECTION",
                                severity="high",
                                message="x",
                            )
                        ],
                    )
                ),
                "error": None,
            }
        ).encode()
        verdict = await check.check(_ctx(), {})
        assert verdict.action == "block"
        assert verdict.reason == "detected"
        assert verdict.findings[0].code == "PROMPT_INJECTION"

    @pytest.mark.asyncio
    async def test_malformed_reply_fails_open_with_critical_finding(
        self,
    ) -> None:
        nc = _FakeNats()
        check = _check()
        check._nc = nc  # type: ignore[attr-defined]
        nc.reply_data = b"this is not JSON"
        verdict = await check.check(_ctx(), {})
        assert verdict.action == "allow"
        assert len(verdict.findings) == 1
        f = verdict.findings[0]
        assert f.severity == "critical"
        assert "RPC_ERROR" in f.code

    @pytest.mark.asyncio
    async def test_reply_with_error_field_fails_open_with_critical_finding(
        self,
    ) -> None:
        nc = _FakeNats()
        check = _check()
        check._nc = nc  # type: ignore[attr-defined]
        nc.reply_data = json.dumps(
            {
                "request_id": "rid",
                "verdict": None,
                "error": "unknown check_id 'x'",
            }
        ).encode()
        verdict = await check.check(_ctx(), {})
        assert verdict.action == "allow"
        assert verdict.findings[0].severity == "critical"
        assert "unknown check_id" in verdict.findings[0].message

    @pytest.mark.asyncio
    async def test_reply_missing_verdict_key_fails_open(self) -> None:
        nc = _FakeNats()
        check = _check()
        check._nc = nc  # type: ignore[attr-defined]
        nc.reply_data = json.dumps(
            {"request_id": "rid", "error": None}
        ).encode()
        verdict = await check.check(_ctx(), {})
        assert verdict.action == "allow"
        assert verdict.findings[0].severity == "critical"


class TestTransportFailures:
    @pytest.mark.asyncio
    async def test_timeout_fails_open_with_timeout_finding(self) -> None:
        nc = _FakeNats()
        nc.raise_on_request = TimeoutError()
        check = _check()
        check._nc = nc  # type: ignore[attr-defined]
        verdict = await check.check(_ctx(), {})
        assert verdict.action == "allow"
        assert verdict.findings[0].code == "SAFETY.RPC_TIMEOUT"
        assert verdict.findings[0].severity == "critical"

    @pytest.mark.asyncio
    async def test_connection_error_fails_open(self) -> None:
        nc = _FakeNats()
        nc.raise_on_request = ConnectionError("nats broken")
        check = _check()
        check._nc = nc  # type: ignore[attr-defined]
        verdict = await check.check(_ctx(), {})
        assert verdict.action == "allow"
        assert verdict.findings[0].code == "SAFETY.RPC_ERROR"


class TestFromSpec:
    def test_from_spec_builds_identical_instance(self) -> None:
        spec: dict[str, Any] = {
            "check_id": "x",
            "version": "2",
            "stages": ["input_prompt", "model_output"],
            "cost_class": "slow",
            "supported_codes": ["A", "B"],
            "default_timeout_ms": 3000,
        }
        nc = _FakeNats()
        rc = RemoteCheck.from_spec(spec, nc)
        assert rc.id == "x"
        assert rc.version == "2"
        assert Stage.INPUT_PROMPT in rc.stages
        assert Stage.MODEL_OUTPUT in rc.stages
        assert rc.cost_class == "slow"
        assert rc.supported_codes == frozenset({"A", "B"})
        assert rc._default_timeout_ms == 3000  # type: ignore[attr-defined]

    def test_from_spec_tolerates_missing_optional_keys(self) -> None:
        # Minimal spec — defaults kick in. Useful when we roll out new
        # fields: old specs on the wire must still deserialize.
        rc = RemoteCheck.from_spec(
            {"check_id": "x", "stages": ["input_prompt"]}, _FakeNats()
        )
        assert rc.version == "1"
        assert rc._default_timeout_ms == 1500  # type: ignore[attr-defined]
