"""SafetyEventsSubscriber — trust boundary unit tests.

Isolates the trusted-tenant lookup logic so the engine tests stay
focused on sink behaviour. Key properties pinned here:

  1. Event with unknown coworker_id  → dropped + log
  2. Event with tenant_id mismatch   → dropped + log
  3. Event with no coworker_id claim → dropped + log
  4. Well-formed event               → forwarded to engine with
                                        authoritative tenant_id and
                                        coworker_id substituted in
  5. Malformed JSON payload          → dropped + log (no crash)

These tests use a fake engine + fake lookup to avoid any DB or NATS
infrastructure; the integration path is covered by the e2e suite.

Design note: the subscriber's trust check is INTENTIONALLY the only
path that validates tenant identity. Placing it here (not in the
engine) keeps the engine a dumb sink, which means engine tests and
subscriber tests exercise orthogonal concerns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from rolemesh.safety.subscriber import SafetyEventsSubscriber, TrustedCoworker


@dataclass
class _FakeCoworker:
    tenant_id: str
    id: str


@dataclass
class _FakeEngine:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def handle_safety_event(self, payload: dict[str, Any]) -> None:
        self.events.append(dict(payload))


def _mk_subscriber(
    known: dict[str, _FakeCoworker],
) -> tuple[SafetyEventsSubscriber, _FakeEngine]:
    engine = _FakeEngine()

    def _lookup(cid: str) -> TrustedCoworker | None:
        return known.get(cid)

    return (
        SafetyEventsSubscriber(engine=engine, coworker_lookup=_lookup),
        engine,
    )


def _valid_payload(
    *,
    tenant_id: str = "tenant-1",
    coworker_id: str = "cw-1",
    stage: str = "pre_tool_call",
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "coworker_id": coworker_id,
        "stage": stage,
        "verdict_action": "block",
        "triggered_rule_ids": ["r-1"],
        "findings": [],
        "context_digest": "a" * 64,
        "context_summary": "tool=x",
    }


class TestTrustBoundary:
    @pytest.mark.asyncio
    async def test_unknown_coworker_id_dropped(self) -> None:
        sub, engine = _mk_subscriber({})
        await sub.on_payload(_valid_payload())
        assert engine.events == []

    @pytest.mark.asyncio
    async def test_empty_coworker_id_dropped(self) -> None:
        sub, engine = _mk_subscriber({"cw-1": _FakeCoworker("t-1", "cw-1")})
        await sub.on_payload(_valid_payload(coworker_id=""))
        assert engine.events == []

    @pytest.mark.asyncio
    async def test_tenant_mismatch_dropped(self) -> None:
        # A buggy or malicious container could claim its tenant is
        # different from the authoritative record. This test is the
        # primary defense against cross-tenant audit poisoning — if
        # it regresses, the isolation property is broken silently.
        sub, engine = _mk_subscriber(
            {"cw-1": _FakeCoworker("trusted-tenant", "cw-1")}
        )
        await sub.on_payload(
            _valid_payload(tenant_id="claimed-other-tenant")
        )
        assert engine.events == [], (
            "subscriber must drop events whose claimed tenant does not "
            "match the in-memory coworker record"
        )

    @pytest.mark.asyncio
    async def test_valid_event_is_forwarded_with_authoritative_ids(
        self,
    ) -> None:
        sub, engine = _mk_subscriber(
            {"cw-1": _FakeCoworker("trusted-tenant", "trusted-cw-id")}
        )
        await sub.on_payload(
            _valid_payload(tenant_id="trusted-tenant", coworker_id="cw-1")
        )
        assert len(engine.events) == 1
        forwarded = engine.events[0]
        # The engine MUST see the authoritative ids, not the claimed
        # ones — even in the benign case where they happen to match,
        # the write path is the same so there's no bypass.
        assert forwarded["tenant_id"] == "trusted-tenant"
        assert forwarded["coworker_id"] == "trusted-cw-id"

    @pytest.mark.asyncio
    async def test_empty_tenant_in_claim_tolerated(self) -> None:
        # Some payloads may omit tenant_id entirely (older container
        # builds, degraded publisher). The subscriber accepts this and
        # fills in the authoritative tenant_id. Only an explicitly
        # wrong tenant_id is rejected.
        sub, engine = _mk_subscriber(
            {"cw-1": _FakeCoworker("trusted-tenant", "cw-1")}
        )
        p = _valid_payload(tenant_id="")
        await sub.on_payload(p)
        assert len(engine.events) == 1
        assert engine.events[0]["tenant_id"] == "trusted-tenant"


class TestJsonDecoding:
    @pytest.mark.asyncio
    async def test_invalid_json_dropped(self) -> None:
        sub, engine = _mk_subscriber({})
        await sub.on_message_bytes(b"{not-json")
        assert engine.events == []

    @pytest.mark.asyncio
    async def test_json_list_dropped(self) -> None:
        # A JSON array is a structurally valid JSON value but not a
        # payload object. Defends against a producer accidentally
        # publishing a batch.
        sub, engine = _mk_subscriber({})
        await sub.on_message_bytes(b'["not", "a", "dict"]')
        assert engine.events == []

    @pytest.mark.asyncio
    async def test_bytes_round_trip_reaches_engine(self) -> None:
        sub, engine = _mk_subscriber(
            {"cw-1": _FakeCoworker("trusted-tenant", "cw-1")}
        )
        payload = _valid_payload(tenant_id="trusted-tenant")
        await sub.on_message_bytes(json.dumps(payload).encode())
        assert len(engine.events) == 1
