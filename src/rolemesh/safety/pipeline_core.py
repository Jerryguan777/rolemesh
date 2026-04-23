"""Shared safety pipeline — runs rules against a SafetyContext.

Lives in ``rolemesh.safety`` (not ``agent_runner.safety``) because both
the container-side hook handler and the orchestrator-side MODEL_OUTPUT
path need to execute the same rule-matching logic. A single entry
point — ``pipeline_run`` — keeps the semantic contract (priority,
short-circuit on block, fail-close on control stages, fail-safe on
observational stages) identical across both call sites.

Semantics (V2 P0.2):

  1. Rules are filtered by ``stage == ctx.stage`` and ``enabled`` and
     (``coworker_id is None`` or ``coworker_id == ctx.coworker_id``).
  2. Remaining rules are sorted by ``priority`` descending.
  3. Rules whose ``check_id`` is not in the registry are skipped with
     a warning — this lets an orchestrator roll back a check without
     breaking in-flight snapshots.
  4. Rules whose stage is not in ``check.stages`` are skipped with a
     log ERROR — defence against a check version that drops a stage
     while old rows still point at it.
  5. Action handling:
       - ``block``            → short-circuit with a block verdict
       - ``require_approval`` → short-circuit; container hook treats
                                 as block for this turn, orchestrator
                                 audit sees ``verdict_action=require_approval``
                                 so P1.1 can create an approval request
       - ``redact``           → replace ``ctx.payload`` with the
                                 ``modified_payload`` from the verdict
                                 and continue; final result is a
                                 ``redact`` verdict carrying the
                                 last-applied payload and a merged
                                 ``appended_context``
       - ``warn``             → accumulate ``appended_context``;
                                 continue without short-circuit
       - ``allow``            → continue
  6. Unknown action from a check is a programming error — fail-close
     on control stages (re-raise), skip on observational.
  7. Check exceptions are re-raised for control stages (INPUT_PROMPT,
     PRE_TOOL_CALL, MODEL_OUTPUT) — the hook bridge translates that
     into a block verdict for the agent. For observational stages the
     exception is logged and the check is skipped. This mirrors the
     fail-mode contract in the HookRegistry docstring.

Audit publish is best-effort: a publisher failure must never alter the
decision, but it is logged at ERROR so operators see ingestion gaps.

Publisher signature: ``Callable[[str, dict], None | Awaitable[None]]``.
Container callers pass a sync publisher (fire-and-forget NATS publish);
the orchestrator passes an async one that awaits a direct sink write.
The pipeline awaits awaitable returns so orch-side audit persistence
completes before the pipeline returns.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .audit import compute_context_digest, summarize_context
from .types import (
    CONTROL_STAGES,
    Finding,
    SafetyContext,
    Verdict,
)

if TYPE_CHECKING:
    from .registry import CheckRegistry

_log = logging.getLogger(__name__)

# Publisher returns None (sync / fire-and-forget) OR an awaitable the
# pipeline will await (used by the orchestrator so its direct-to-sink
# write completes before pipeline_run returns). Kept as a single type
# so callers don't have to pick between two overloads.
AuditPublisher = Callable[[str, dict[str, Any]], None | Awaitable[None]]

# Backwards-compat alias. Some call sites still prefer to declare an
# async-only signature; the pipeline itself does not distinguish.
AsyncAuditPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]

# Subject template for audit events. Orchestrator subscribes with a
# wildcard ``agent.*.safety_events``. Orchestrator-side publishers
# bypass NATS entirely, so the subject string is only meaningful on
# the container path.
AUDIT_SUBJECT_TEMPLATE = "agent.{job_id}.safety_events"


# Actions the pipeline can translate into hook verdicts. A check that
# returns anything outside this set is a programming error — fail-close
# on control stages (re-raise), skip on observational.
_V2_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"allow", "block", "redact", "warn", "require_approval"}
)

# Actions that short-circuit the pipeline (no later rule runs). Redact
# and warn deliberately do NOT short-circuit — a downstream rule may
# still turn into a block on the already-modified payload, and warn is
# purely additive.
_SHORT_CIRCUIT_ACTIONS: frozenset[str] = frozenset({"block", "require_approval"})


async def pipeline_run(
    rules: list[dict[str, Any]],
    registry: CheckRegistry,
    ctx: SafetyContext,
    publisher: AuditPublisher,
) -> Verdict:
    applicable = [r for r in rules if _rule_applies(r, ctx)]
    applicable.sort(key=lambda r: -int(r.get("priority", 100)))

    all_findings: list[Finding] = []
    # Redact chain state. ``current_ctx`` is what the NEXT rule will see
    # — every redact verdict replaces its payload. ``redact_happened``
    # captures whether any rule actually redacted so the final tail
    # verdict can correctly encode the chained modifications.
    current_ctx = ctx
    redact_happened = False
    # Warn accumulator — joined with \n\n at the tail so the hook sees
    # one combined context string rather than a list.
    warn_contexts: list[str] = []

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
        if current_ctx.stage not in check.stages:
            _log.error(
                "safety: rule stage not in check.stages — skipping",
                extra={
                    "check_id": check_id,
                    "rule_id": rule.get("id"),
                    "stage": current_ctx.stage.value,
                    "check_stages": sorted(s.value for s in check.stages),
                },
            )
            continue

        # V2 P0.4: reversibility matrix — slow checks on reversible
        # tools at PRE_TOOL_CALL exceed the 100 ms budget for no
        # safety benefit (the tool has no lasting side effect). Skip
        # with a log ERROR so a misconfigured rule is visible, but
        # keep the rest of the pipeline running: other rules at this
        # stage may still be valid. We only apply the guard when the
        # tool's reversibility is known (``ctx.tool.reversible``);
        # POST_TOOL_RESULT and other stages have their own budgets.
        if (
            current_ctx.stage.value == "pre_tool_call"
            and getattr(check, "cost_class", "cheap") == "slow"
            and current_ctx.tool is not None
            and current_ctx.tool.reversible
        ):
            _log.error(
                "safety: slow check on reversible tool at "
                "PRE_TOOL_CALL — skipping",
                extra={
                    "check_id": check_id,
                    "rule_id": rule.get("id"),
                    "tool": current_ctx.tool.name,
                },
            )
            continue

        rule_config = rule.get("config") or {}
        if not isinstance(rule_config, dict):
            rule_config = {}

        try:
            verdict = await check.check(current_ctx, rule_config)
        except Exception as exc:
            if current_ctx.stage in CONTROL_STAGES:
                # Fail-close: re-raise so the hook bridge converts
                # the exception into a block verdict for the agent.
                _log.warning(
                    "safety: check raised on control stage — failing closed",
                    extra={
                        "check_id": check_id,
                        "rule_id": rule.get("id"),
                        "stage": current_ctx.stage.value,
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
                    "stage": current_ctx.stage.value,
                    "error": str(exc),
                },
            )
            continue

        if verdict.action not in _V2_ALLOWED_ACTIONS:
            msg = (
                f"check {check_id!r} returned unsupported action "
                f"{verdict.action!r}"
            )
            if current_ctx.stage in CONTROL_STAGES:
                raise ValueError(msg)
            _log.error("safety: %s", msg)
            continue

        all_findings.extend(verdict.findings)

        # Redact MUST carry a modified_payload. A check that returns
        # redact without one is a programming error — there is no way
        # to continue the chain without the replacement payload. Treat
        # the same as any unsupported action: fail-close on control,
        # skip on observational.
        if verdict.action == "redact" and verdict.modified_payload is None:
            msg = (
                f"check {check_id!r} returned redact without "
                f"modified_payload"
            )
            if current_ctx.stage in CONTROL_STAGES:
                raise ValueError(msg)
            _log.error("safety: %s", msg)
            continue

        await _publish_audit(publisher, current_ctx, rule, verdict)

        if verdict.action in _SHORT_CIRCUIT_ACTIONS:
            # block + require_approval both exit the loop. The caller
            # (hook or orch) distinguishes between them via
            # verdict.action: block → refuse; require_approval → emit
            # approval request (handled on the orch side in P1.1) and
            # also refuse on the container side for this turn.
            return replace(verdict, findings=list(all_findings))

        if verdict.action == "redact":
            # Swap the ctx payload so the next rule sees the modified
            # view. Frozen dataclass → use replace() to build a new one.
            modified = verdict.modified_payload
            if not isinstance(modified, dict):
                # Defensive: checks MUST return a dict shaped for the
                # stage. Skip on mismatch rather than crash.
                _log.error(
                    "safety: check %r returned non-dict modified_payload",
                    check_id,
                )
                continue
            current_ctx = replace(current_ctx, payload=dict(modified))
            redact_happened = True
            if verdict.appended_context:
                warn_contexts.append(verdict.appended_context)
            continue

        if verdict.action == "warn":
            if verdict.appended_context:
                warn_contexts.append(verdict.appended_context)
            continue

        # allow — continue evaluating remaining rules.

    # End of rule chain — synthesize the final verdict from accumulated
    # state. redact beats warn beats allow in terms of "which action
    # the hook sees", but findings and appended_context are preserved
    # across all outcomes.
    combined_context = "\n\n".join(warn_contexts) if warn_contexts else None
    if redact_happened:
        return Verdict(
            action="redact",
            modified_payload=dict(current_ctx.payload),
            findings=list(all_findings),
            appended_context=combined_context,
        )
    if combined_context is not None:
        return Verdict(
            action="warn",
            findings=list(all_findings),
            appended_context=combined_context,
        )
    return Verdict(action="allow", findings=list(all_findings))


def _rule_applies(rule: dict[str, Any], ctx: SafetyContext) -> bool:
    if not rule.get("enabled", True):
        return False
    if str(rule.get("stage", "")) != ctx.stage.value:
        return False
    scope = rule.get("coworker_id")
    return not (scope is not None and scope != ctx.coworker_id)


async def _publish_audit(
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
        result = publisher(subject, event)
        if inspect.isawaitable(result):
            # Async publisher (orchestrator direct-to-sink path) — await
            # so audit persistence completes before pipeline_run returns.
            # Sync publishers (container fire-and-forget) return None and
            # skip the await entirely.
            await result
    except Exception as exc:  # noqa: BLE001 — audit must never block decision
        _log.error(
            "safety: audit publish failed",
            extra={
                "rule_id": rule_id,
                "subject": subject,
                "error": str(exc),
            },
        )


__all__ = [
    "AUDIT_SUBJECT_TEMPLATE",
    "AsyncAuditPublisher",
    "AuditPublisher",
    "pipeline_run",
]
