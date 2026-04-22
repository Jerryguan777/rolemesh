"""Container-side pipeline that executes rules against a SafetyContext.

A single call to ``pipeline_run`` is the only entry point. The hook
handler builds the ``SafetyContext`` from a ``ToolCallEvent`` and
forwards it here; everything else — rule filtering, priority ordering,
short-circuiting on block, redact chaining, audit event publishing —
happens inside this function.

Semantic guarantees:

  1. Rules are filtered by ``stage == ctx.stage`` and ``enabled`` and
     (``coworker_id is None`` or ``coworker_id == ctx.coworker_id``).
  2. Remaining rules are sorted by ``priority`` descending.
  3. Rules whose ``check_id`` is not in the registry are skipped with a
     warning — this lets an orchestrator roll back a check without
     breaking in-flight snapshots.
  4. On a ``block`` verdict the pipeline publishes one audit event and
     returns immediately (short-circuit).
  5. Redact verdicts chain: each successive check sees the modified
     payload produced by the previous redact. V1 does not exercise this
     path (config is always block) but the structure is in place so V2
     can switch ``action`` without rewriting the pipeline.
  6. Check exceptions are re-raised for control stages (INPUT_PROMPT,
     PRE_TOOL_CALL, MODEL_OUTPUT) — the hook bridge translates that
     into a block verdict for the agent. For observational stages the
     exception is logged and the check is skipped. This mirrors the
     fail-mode contract in the HookRegistry docstring.

Audit publish is fire-and-forget: a NATS failure must never alter the
decision, but it is logged at ERROR so operators see ingestion gaps.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from rolemesh.safety.audit import compute_context_digest, summarize_context
from rolemesh.safety.types import (
    CONTROL_STAGES,
    Finding,
    SafetyContext,
    Verdict,
)

if TYPE_CHECKING:
    from rolemesh.safety.registry import CheckRegistry

_log = logging.getLogger(__name__)

# Callable the pipeline uses to publish audit events. A sync function
# that queues a fire-and-forget NATS publish — the ``ToolContext.publish``
# method on the agent side already matches this signature.
AuditPublisher = Callable[[str, dict[str, Any]], None]

# Subject template for audit events. Orchestrator subscribes with a
# wildcard ``agent.*.safety_events``.
AUDIT_SUBJECT_TEMPLATE = "agent.{job_id}.safety_events"


async def pipeline_run(
    rules: list[dict[str, Any]],
    registry: CheckRegistry,
    ctx: SafetyContext,
    publisher: AuditPublisher,
) -> Verdict:
    applicable = [r for r in rules if _rule_applies(r, ctx)]
    applicable.sort(key=lambda r: -int(r.get("priority", 100)))

    all_findings: list[Finding] = []
    current_ctx = ctx
    accumulated_payload: Any = None

    for rule in applicable:
        check_id = str(rule.get("check_id", ""))
        if not registry.has(check_id):
            _log.warning(
                "safety: unknown check_id in snapshot — skipping",
                extra={"check_id": check_id, "rule_id": rule.get("id")},
            )
            continue

        check = registry.get(check_id)
        # Defensive: REST validates (stage, check_id) compatibility at
        # creation time, but a rule can become invalid if the check is
        # upgraded to drop a stage, or if an operator edits the row
        # directly. Skip rather than hand the check a payload type it
        # does not know how to interpret.
        if ctx.stage not in check.stages:
            _log.error(
                "safety: rule stage not in check.stages — skipping",
                extra={
                    "check_id": check_id,
                    "rule_id": rule.get("id"),
                    "stage": ctx.stage.value,
                    "check_stages": sorted(s.value for s in check.stages),
                },
            )
            continue

        rule_config = rule.get("config") or {}
        if not isinstance(rule_config, dict):
            rule_config = {}

        try:
            verdict = await check.check(current_ctx, rule_config)
        except Exception as exc:
            if ctx.stage in CONTROL_STAGES:
                # Fail-close: re-raise so the hook bridge converts
                # the exception into a block verdict for the agent.
                _log.warning(
                    "safety: check raised on control stage — failing closed",
                    extra={
                        "check_id": check_id,
                        "rule_id": rule.get("id"),
                        "stage": ctx.stage.value,
                        "error": str(exc),
                    },
                )
                raise
            # Observational: fail-safe, skip this rule and continue.
            _log.warning(
                "safety: check raised on observational stage — skipping",
                extra={
                    "check_id": check_id,
                    "rule_id": rule.get("id"),
                    "stage": ctx.stage.value,
                    "error": str(exc),
                },
            )
            continue

        all_findings.extend(verdict.findings)

        if verdict.action == "block":
            _publish_audit(publisher, current_ctx, rule, verdict)
            return replace(verdict, findings=list(all_findings))

        if verdict.action == "redact" and verdict.modified_payload is not None:
            accumulated_payload = verdict.modified_payload
            # Feed the redacted payload to the next check so a second
            # rule can scan on the cleaned-up content.
            if isinstance(accumulated_payload, dict):
                current_ctx = replace(current_ctx, payload=accumulated_payload)
            _publish_audit(publisher, ctx, rule, verdict)
            continue

        _publish_audit(publisher, ctx, rule, verdict)

    if accumulated_payload is not None:
        return Verdict(
            action="redact",
            modified_payload=accumulated_payload,
            findings=list(all_findings),
        )
    return Verdict(action="allow", findings=list(all_findings))


def _rule_applies(rule: dict[str, Any], ctx: SafetyContext) -> bool:
    if not rule.get("enabled", True):
        return False
    if str(rule.get("stage", "")) != ctx.stage.value:
        return False
    scope = rule.get("coworker_id")
    return not (scope is not None and scope != ctx.coworker_id)


def _publish_audit(
    publisher: AuditPublisher,
    ctx: SafetyContext,
    rule: dict[str, Any],
    verdict: Verdict,
) -> None:
    rule_id = str(rule.get("id") or "")
    if not rule_id:
        return
    event: dict[str, Any] = {
        "tenant_id": ctx.tenant_id,
        "coworker_id": ctx.coworker_id or None,
        "conversation_id": ctx.conversation_id or None,
        "job_id": ctx.job_id or None,
        "stage": ctx.stage.value,
        "verdict_action": verdict.action,
        "triggered_rule_ids": [rule_id],
        "findings": [
            {
                "code": f.code,
                "severity": f.severity,
                "message": f.message,
                "metadata": dict(f.metadata),
            }
            for f in verdict.findings
        ],
        "context_digest": compute_context_digest(ctx.payload),
        "context_summary": summarize_context(ctx.stage.value, ctx.payload),
    }
    subject = AUDIT_SUBJECT_TEMPLATE.format(job_id=ctx.job_id or "unknown")
    try:
        publisher(subject, event)
    except Exception as exc:  # noqa: BLE001 — audit must never block decision
        _log.error(
            "safety: audit publish failed",
            extra={
                "rule_id": rule_id,
                "subject": subject,
                "error": str(exc),
            },
        )


# Await-compatible signature so pi_backend's Awaitable-returning publisher
# stays feasible without a second flavour. Explicit typing lets mypy keep
# --strict happy in call sites where the publisher is declared Any-typed.
AsyncAuditPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]


__all__ = [
    "AUDIT_SUBJECT_TEMPLATE",
    "AsyncAuditPublisher",
    "AuditPublisher",
    "pipeline_run",
]
