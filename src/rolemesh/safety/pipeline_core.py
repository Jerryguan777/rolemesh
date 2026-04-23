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

        # V2 P1.1: ``action_override`` on the rule config lets an
        # operator up/down-grade the check's natural verdict — e.g.
        # turning an SSN block into an approval request without
        # writing a new check. ``redact`` is forbidden as an override
        # because it requires the check to have produced a
        # ``modified_payload``; REST validation rejects it there, but
        # defensive runtime handling catches a rogue direct INSERT.
        override = rule_config.get("action_override")
        if override is not None and override not in (
            "block",
            "warn",
            "require_approval",
        ):
            # Unknown override value — REST should have rejected it,
            # but if a direct-to-DB rule slips through we refuse to
            # apply an unsupported action rather than silently ignoring
            # it. Skip the rule so its misconfiguration is visible.
            _log.error(
                "safety: rule has invalid action_override — skipping",
                extra={
                    "rule_id": rule.get("id"),
                    "check_id": check_id,
                    "override": str(override),
                },
            )
            continue

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

        # Apply the override only when the check's natural verdict
        # was non-allow — overriding an allow to block/approval would
        # make every evaluation a gate regardless of whether the
        # check actually detected anything.
        if override is not None and verdict.action != "allow":
            verdict = replace(verdict, action=override)

        # Redact MUST carry a dict-shaped modified_payload. Both
        # ``None`` and a non-dict are programming errors — treat them
        # identically so the audit is consistent with what actually
        # happens (either "row + effect" or "no row + no effect",
        # never "row says redact but nothing was replaced"). Before
        # the review fix, None short-circuited here but non-dict
        # slipped through the audit publish below.
        if verdict.action == "redact" and not isinstance(
            verdict.modified_payload, dict
        ):
            msg = (
                f"check {check_id!r} returned redact with "
                f"{type(verdict.modified_payload).__name__} "
                f"modified_payload (expected dict)"
            )
            if current_ctx.stage in CONTROL_STAGES:
                raise ValueError(msg)
            _log.error("safety: %s", msg)
            continue

        all_findings.extend(verdict.findings)

        await _publish_audit(publisher, current_ctx, rule, verdict)

        if verdict.action in _SHORT_CIRCUIT_ACTIONS:
            # block + require_approval both exit the loop. The caller
            # (hook or orch) distinguishes between them via
            # verdict.action: block → refuse; require_approval → emit
            # approval request (handled on the orch side in P1.1) and
            # also refuse on the container side for this turn.
            return replace(verdict, findings=list(all_findings))

        if verdict.action == "redact":
            # Shape already validated above. Swap the ctx payload so
            # the next rule sees the modified view. Frozen dataclass →
            # use replace() to build a new one.
            modified = verdict.modified_payload
            assert isinstance(modified, dict)  # for mypy
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
        # V2 P1.1: user_id threads through so the approval bridge
        # (SafetyEngine._dispatch_require_approval →
        # ApprovalEngine.create_from_safety) can attribute the
        # request to the user whose turn triggered the gate.
        # AuditEvent stays the source of truth for this field across
        # the subscriber → engine → approval fan-out.
        "user_id": ctx.user_id or None,
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
    # V2 P1.1: attach approval_context so the orch audit handler can
    # create an approval_request without re-deriving the tool_input.
    # Only on PRE_TOOL_CALL — the other stages don't map onto the
    # approval module's {mcp_server, tool_name, params} schema.
    if (
        verdict.action == "require_approval"
        and ctx.stage.value == "pre_tool_call"
    ):
        tool_name = str(ctx.payload.get("tool_name", ""))
        tool_input_raw = ctx.payload.get("tool_input") or {}
        tool_input = (
            dict(tool_input_raw)
            if isinstance(tool_input_raw, dict)
            else {}
        )
        # mcp_server_name parsed from ``mcp__{server}__{tool}``; empty
        # for stock Claude tools (which never round-trip through the
        # MCP proxy anyway).
        mcp_server_name = ""
        if tool_name.startswith("mcp__") and tool_name.count("__") >= 2:
            mcp_server_name = tool_name.split("__", 2)[1]
        event["approval_context"] = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "mcp_server_name": mcp_server_name,
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
