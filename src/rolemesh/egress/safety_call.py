"""Lightweight Safety pipeline for the egress gateway (EC-2).

The gateway's EGRESS_REQUEST stage sees two very different shapes —
a forward-proxy CONNECT and a DNS query — but both resolve to the same
question: "is this (tenant, coworker, host, port, mode) allowed?".

Aggregation semantics
---------------------

This deliberately inverts the default pipeline's allow-unless-blocked
rule. For egress the default is DENY and explicit allowlist rules
punch holes:

    no rule matches          → block (default deny)
    any rule matched (allow) → allow overall

That contract lives here (not in individual checks) so adding new EGRESS
check types later — e.g. a V2 ``egress.content_scanner`` that redacts —
doesn't have to reason about "am I the last voter?". Each check reports
whether it matched; the caller aggregates.

Audit surface
-------------

Every decision, allow OR block, writes one row into
``safety_decisions`` via the orchestrator's existing
``agent.<job_id>.safety_events`` fan-in. Publishing is
fire-and-forget — an audit-sink outage must not stall the network
hot path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    import nats.aio.client

    from .identity import Identity
    from .policy_cache import PolicyCache

logger = get_logger()


# Mirrors safety/types.py. Hard-coded here (rather than imported) so this
# module does not pull the full rolemesh.safety.types dependency into
# the gateway container — EC-2 keeps the gateway image lean. EC-3 adds
# ``Stage.EGRESS_REQUEST`` to the enum; we use the string form on the
# wire regardless.
EGRESS_REQUEST_STAGE = "egress_request"

EgressMode = Literal["forward", "reverse", "dns"]
EgressAction = Literal["allow", "block"]


@dataclass(frozen=True)
class EgressRequest:
    """Pre-decoded view of an outbound request for the safety pipeline."""

    host: str
    port: int
    mode: EgressMode
    method: str | None = None  # HTTP verb for forward/reverse; None for DNS
    qtype: str | None = None   # DNS query type; None for HTTP


@dataclass(frozen=True)
class EgressDecision:
    """Result returned to the forward-proxy / DNS resolver."""

    action: EgressAction
    reason: str
    triggered_rule_ids: list[str] = field(default_factory=list)
    # Individual check findings in dict-shape. The audit publisher
    # serializes this straight through; the orchestrator subscriber
    # drops it into ``safety_decisions.findings`` as JSONB.
    findings: list[dict[str, Any]] = field(default_factory=list)


# Check implementations live in safety/checks/egress_domain_rule.py
# (registered in EC-3). The gateway imports them by check_id via the
# same mechanism the orchestrator uses — a dict keyed on string.
#
# Type is Any to avoid the hard import dependency in EC-2. A production
# deployment loads the concrete check modules at gateway startup.
CheckFunc = Any


class EgressSafetyCaller:
    """Evaluates an EgressRequest against the policy cache + writes audit.

    One instance per gateway process; not thread-safe, which matches
    the asyncio single-threaded model.
    """

    def __init__(
        self,
        *,
        cache: PolicyCache,
        checks: dict[str, CheckFunc],
        audit_publisher: AuditPublisher,
    ) -> None:
        self._cache = cache
        self._checks = checks
        self._audit = audit_publisher
        self._audit_tasks: set[asyncio.Task[None]] = set()

    async def decide(
        self,
        *,
        identity: Identity | None,
        request: EgressRequest,
    ) -> EgressDecision:
        """Evaluate all applicable rules and return the aggregate decision.

        ``identity is None`` → block unconditionally. This is the
        unknown-source-IP path discussed in identity.py: the gateway
        will NOT default to a tenant.
        """
        if identity is None:
            decision = EgressDecision(
                action="block",
                reason="Unknown source identity",
            )
            # Unknown identity means we cannot attribute the audit row
            # to any tenant. Skipping the audit here (rather than
            # writing to a "system" tenant) matches the multi-tenant
            # isolation guarantee in subscriber.py: every row must
            # come from an authenticated coworker.
            return decision

        rules = self._cache.get_rules_for(identity.tenant_id, identity.coworker_id)
        findings: list[dict[str, Any]] = []
        triggered: list[str] = []

        for rule in rules:
            if rule.stage != EGRESS_REQUEST_STAGE:
                continue
            check = self._checks.get(rule.check_id)
            if check is None:
                # A rule referencing an unknown check is a config error;
                # surface it as a structured WARN rather than silently
                # treat it as a hit.
                logger.warning(
                    "egress safety: unknown check — skipping rule",
                    rule_id=rule.id,
                    check_id=rule.check_id,
                )
                continue
            try:
                matched, rule_findings = await check(request, rule.config)
            except Exception as exc:  # noqa: BLE001 — check code must not crash gateway
                logger.error(
                    "egress safety: check raised — skipping rule",
                    rule_id=rule.id,
                    check_id=rule.check_id,
                    error=str(exc),
                )
                continue
            if matched:
                triggered.append(rule.id)
                findings.extend(rule_findings)

        if triggered:
            decision = EgressDecision(
                action="allow",
                reason=f"Matched {len(triggered)} allowlist rule(s)",
                triggered_rule_ids=triggered,
                findings=findings,
            )
        else:
            decision = EgressDecision(
                action="block",
                reason=f"No egress allowlist rule matched {request.host}:{request.port}",
            )

        # Audit publish is fire-and-forget. A slow audit sink cannot
        # be allowed to stall the CONNECT/DNS hot path. Pin the task on
        # the caller so the GC doesn't reap it mid-publish.
        task = asyncio.create_task(
            self._audit.publish(identity=identity, request=request, decision=decision)
        )
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

        return decision


@dataclass
class AuditPublisher:
    """Emits one safety_event per gateway decision onto the existing
    ``agent.<job_id>.safety_events`` fan-in.

    The subscriber on the orchestrator side re-validates the claimed
    coworker_id against the in-memory state before writing, which
    prevents a compromised gateway (or a misconfigured test gateway)
    from forging audit rows for someone else's tenant.
    """

    nats_client: nats.aio.client.Client

    async def publish(
        self,
        *,
        identity: Identity,
        request: EgressRequest,
        decision: EgressDecision,
    ) -> None:
        payload_for_digest = {
            "host": request.host,
            "port": request.port,
            "mode": request.mode,
            "qtype": request.qtype,
            "method": request.method,
        }
        digest = hashlib.sha256(
            json.dumps(payload_for_digest, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Short summary — matches the pattern in
        # safety/audit.py::summarize_context. Keeps the safety_decisions
        # row human-scannable without storing the full target.
        summary = f"{request.mode}:{request.host}:{request.port}"
        if request.qtype:
            summary = f"{summary} qtype={request.qtype}"

        event = {
            "tenant_id": identity.tenant_id,
            "coworker_id": identity.coworker_id,
            "user_id": identity.user_id,
            "conversation_id": identity.conversation_id,
            "job_id": identity.job_id,
            "stage": EGRESS_REQUEST_STAGE,
            "verdict_action": decision.action,
            "triggered_rule_ids": decision.triggered_rule_ids,
            "findings": _finalize_findings(decision.findings, request),
            "context_digest": digest,
            "context_summary": summary,
        }
        subject = f"agent.{identity.job_id or 'unknown'}.safety_events"
        try:
            await self.nats_client.publish(  # type: ignore[attr-defined]
                subject, json.dumps(event).encode("utf-8")
            )
        except Exception as exc:  # noqa: BLE001 — audit publish must not cascade
            logger.error(
                "egress safety: audit publish failed",
                component="safety",
                subject=subject,
                error=str(exc),
            )


def _finalize_findings(
    findings: list[dict[str, Any]], request: EgressRequest
) -> list[dict[str, Any]]:
    """Ensure every finding carries the mode/qtype context so operators
    can filter audit rows by DNS vs forward vs reverse without joining
    on another column.

    Mutating the incoming dicts would surprise callers; we always
    return a new list.
    """
    out: list[dict[str, Any]] = []
    for f in findings:
        augmented = dict(f)
        augmented.setdefault("metadata", {})
        if not isinstance(augmented["metadata"], dict):
            augmented["metadata"] = {"raw": augmented["metadata"]}
        augmented["metadata"]["mode"] = request.mode
        if request.qtype:
            augmented["metadata"]["qtype"] = request.qtype
        out.append(augmented)
    return out
