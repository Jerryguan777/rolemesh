"""Tests for the gateway-local Safety pipeline.

The decision invariant under test: no rule matches → block. Any rule
matches → allow. These tests also verify that unknown identity =
block and audit publishing never stalls the decision path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from rolemesh.egress.identity import Identity
from rolemesh.egress.policy_cache import PolicyCache
from rolemesh.egress.safety_call import (
    AuditPublisher,
    EgressRequest,
    EgressSafetyCaller,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeNats:
    """Captures publish calls without hitting real NATS."""

    published: list[tuple[str, dict[str, Any]]]

    async def publish(self, subject: str, data: bytes) -> None:
        import json

        self.published.append((subject, json.loads(data)))


def _identity() -> Identity:
    return Identity(
        tenant_id="tenant-a",
        coworker_id="coworker-x",
        user_id="user-1",
        conversation_id="conv-1",
        job_id="job-1",
        container_name="rolemesh-foo-1",
    )


async def _make_caller(
    *,
    rules: list[dict[str, Any]],
    checks: dict[str, Any],
    nc: _FakeNats,
) -> EgressSafetyCaller:
    cache = PolicyCache()
    await cache.seed(rules)
    audit = AuditPublisher(nats_client=nc)  # type: ignore[arg-type]
    return EgressSafetyCaller(cache=cache, checks=checks, audit_publisher=audit)


async def test_no_rules_blocks_by_default() -> None:
    """Default-deny is the whole point of the egress pipeline."""
    nc = _FakeNats(published=[])
    caller = await _make_caller(rules=[], checks={}, nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    assert decision.action == "block"


async def test_matching_rule_allows() -> None:
    """At least one rule matched → allow, even if other rules are silent."""
    async def _always_match(
        request: EgressRequest, config: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        return True, []

    rules = [
        {
            "id": "r1",
            "rule_id": "r1",
            "tenant_id": "tenant-a",
            "coworker_id": "coworker-x",
            "stage": "egress_request",
            "check_id": "egress.domain_rule",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
    ]
    nc = _FakeNats(published=[])
    caller = await _make_caller(
        rules=rules, checks={"egress.domain_rule": _always_match}, nc=nc
    )
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    assert decision.action == "allow"
    assert decision.triggered_rule_ids == ["r1"]


async def test_unknown_identity_always_blocks() -> None:
    """Unknown source IP must never default to any tenant."""
    nc = _FakeNats(published=[])
    caller = await _make_caller(rules=[], checks={}, nc=nc)
    decision = await caller.decide(
        identity=None,
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    assert decision.action == "block"
    assert "Unknown source identity" in decision.reason


async def test_check_exception_does_not_crash_decide() -> None:
    """A buggy check must not kill the gateway hot path."""
    async def _crashes(
        request: EgressRequest, config: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        raise RuntimeError("boom")

    rules = [
        {
            "id": "r1",
            "rule_id": "r1",
            "tenant_id": "tenant-a",
            "coworker_id": "coworker-x",
            "stage": "egress_request",
            "check_id": "egress.domain_rule",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
    ]
    nc = _FakeNats(published=[])
    caller = await _make_caller(
        rules=rules, checks={"egress.domain_rule": _crashes}, nc=nc
    )
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    assert decision.action == "block"  # crashed check = no match = block


async def test_unknown_check_id_is_skipped() -> None:
    """A rule referencing a check_id we don't have is a config error —
    skip it rather than treating it as a hit (which would silently
    grant egress)."""
    rules = [
        {
            "id": "r1",
            "rule_id": "r1",
            "tenant_id": "tenant-a",
            "coworker_id": "coworker-x",
            "stage": "egress_request",
            "check_id": "egress.does_not_exist",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
    ]
    nc = _FakeNats(published=[])
    caller = await _make_caller(rules=rules, checks={}, nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    assert decision.action == "block"
