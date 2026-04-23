"""Orchestrator-side SafetyEngine.

Thin façade shared by three call sites:

  - ``load_rules_for_coworker`` — snapshot the ``safety_rules`` rows
    for admin-side / API inspection. Raises on DB failure so callers
    see the error rather than silently receiving an empty list.
    Container startup does NOT use this path — it uses
    ``rolemesh.safety.loader.load_safety_rules_snapshot``, which
    layers ``SAFETY_FAIL_MODE`` dispatch on top of the same query.
  - ``handle_safety_event`` — decode a NATS payload into an
    ``AuditEvent`` and forward it to the configured sink. Caller is
    responsible for trust checking (see ``SafetyEventsSubscriber``).
  - ``run_orchestrator_pipeline`` — execute the shared pipeline for
    stages the orchestrator itself produces (V2 P0.1: MODEL_OUTPUT).
    Bypasses NATS since the tenant identity comes from server-side
    state; audit rows are written directly to the sink.

Both rule-reading paths share ``fetch_safety_rule_snapshots`` in
loader.py so the query + serialization live in one place; only the
error handling differs.

The engine deliberately holds no state; rule reloads are just fresh
queries. V2 adds RPC support via a separate ``SafetyRpcServer`` class
rather than bolting it onto this façade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

from .audit import AuditEvent, AuditSink, DbAuditSink

if TYPE_CHECKING:
    from .types import SafetyContext, Verdict

logger = get_logger()


class SafetyEngine:
    def __init__(
        self,
        *,
        audit_sink: AuditSink | None = None,
        approval_handler: Any = None,
    ) -> None:
        self._sink: AuditSink = audit_sink or DbAuditSink()
        # V2 P1.1: optional dependency so a deployment without the
        # approval module still works. ``approval_handler`` is any
        # callable/object with an async ``create_from_safety`` method
        # (see ``rolemesh.approval.engine.ApprovalEngine.create_from_safety``).
        # Typed as Any to keep this module importable without the
        # approval package at cold start.
        self._approval_handler = approval_handler

    async def load_rules_for_coworker(
        self, tenant_id: str, coworker_id: str
    ) -> list[dict[str, Any]]:
        """Return the enabled rules for a coworker as container-ready dicts.

        Raises on DB failure — this path is for admin / API callers
        that NEED to see the error (e.g. a 500 back to the UI), not
        for container startup. Container startup uses
        ``load_safety_rules_snapshot`` which applies ``SAFETY_FAIL_MODE``.

        Thin wrapper around ``fetch_safety_rule_snapshots`` — the
        shared helper makes the two paths share a query body so they
        cannot drift on things like field selection, enabled-filter
        semantics, or dict shape.
        """
        from .loader import fetch_safety_rule_snapshots

        return await fetch_safety_rule_snapshots(tenant_id, coworker_id)

    async def handle_safety_event(self, payload: dict[str, Any]) -> None:
        """Persist one already-validated safety event.

        Caller (typically ``SafetyEventsSubscriber``) MUST have already
        replaced the payload's ``tenant_id`` / ``coworker_id`` with
        authoritative values obtained from an in-memory coworker
        lookup. This method does not re-validate — it only writes. The
        trust boundary lives at the subscriber, not here, so unit
        tests can separate "is the tenant check correct?" from "is
        the sink write correct?".

        Malformed payloads (missing required keys after validation)
        are dropped with a warning.
        """
        try:
            approval_ctx = payload.get("approval_context")
            event = AuditEvent(
                tenant_id=str(payload["tenant_id"]),
                coworker_id=payload.get("coworker_id"),
                conversation_id=payload.get("conversation_id"),
                job_id=payload.get("job_id"),
                user_id=payload.get("user_id"),
                stage=str(payload["stage"]),
                verdict_action=str(payload["verdict_action"]),
                triggered_rule_ids=list(payload.get("triggered_rule_ids") or []),
                findings=list(payload.get("findings") or []),
                context_digest=str(payload.get("context_digest", "")),
                context_summary=str(payload.get("context_summary", "")),
                approval_context=(
                    dict(approval_ctx)
                    if isinstance(approval_ctx, dict)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "safety: dropping malformed safety event",
                component="safety",
                error=str(exc),
            )
            return

        try:
            await self._sink.write(event)
        except Exception as exc:  # noqa: BLE001 — sink failure must not cascade
            logger.error(
                "safety: audit sink write failed",
                component="safety",
                tenant_id=event.tenant_id,
                stage=event.stage,
                verdict=event.verdict_action,
                error=str(exc),
            )

        # V2 P1.1: bridge require_approval verdicts into the approval
        # module. The check ran on the container, the audit already
        # landed; this step creates a human-in-the-loop decision
        # surface (WebUI / Slack notification) tied to the same
        # tenant/coworker/conversation. Engine-less deployments (no
        # approval module) log and skip — the audit row alone is
        # still valuable for operators.
        if (
            event.verdict_action == "require_approval"
            and event.approval_context is not None
        ):
            await self._dispatch_require_approval(event)

    async def _dispatch_require_approval(
        self, event: AuditEvent
    ) -> None:
        """Create an approval_request from a safety require_approval audit.

        Audit-first / approval-second ordering is deliberate: the audit
        row is the compliance record ("at time T, rule X decided this
        required approval"). The approval request is a consumer of
        that record for human UI. If approval creation fails (module
        down, DB hiccup), the audit still stands, and operators can
        see the backlog via safety_decisions filters.
        """
        if self._approval_handler is None:
            logger.warning(
                "safety: require_approval event has no approval handler "
                "— audit row written but no approval request created",
                component="safety",
                tenant_id=event.tenant_id,
                coworker_id=event.coworker_id,
                job_id=event.job_id,
            )
            return
        ctx = event.approval_context or {}
        try:
            await self._approval_handler.create_from_safety(
                tenant_id=event.tenant_id,
                coworker_id=event.coworker_id or "",
                conversation_id=event.conversation_id,
                job_id=event.job_id or "",
                user_id=event.user_id or "",
                tool_name=str(ctx.get("tool_name") or ""),
                tool_input=dict(ctx.get("tool_input") or {}),
                mcp_server_name=str(ctx.get("mcp_server_name") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — approval creation must not cascade
            logger.error(
                "safety: approval creation from require_approval failed",
                component="safety",
                tenant_id=event.tenant_id,
                coworker_id=event.coworker_id,
                error=str(exc),
            )

    async def run_orchestrator_pipeline(
        self,
        ctx: SafetyContext,
        rules: list[dict[str, Any]],
    ) -> Verdict:
        """Execute the shared pipeline for orchestrator-produced input.

        Used for stages the server itself drives — currently only
        MODEL_OUTPUT. The tenant identity in ``ctx`` comes from the
        orchestrator's in-memory coworker state, so no NATS round-trip
        or trust check is needed; audit rows are written directly to
        the sink via an async publisher that the pipeline awaits.

        Zero-rule calls short-circuit before touching the registry or
        sink, preserving the "no rules → zero overhead" invariant for
        the hot path every turn goes through.

        Returns the final ``Verdict``. The orchestrator caller is
        responsible for translating block / redact into a modified
        ``AgentOutput`` — this method does not mutate anything outside
        the audit sink.
        """
        from .pipeline_core import pipeline_run
        from .registry import get_orchestrator_registry
        from .types import Verdict

        if not rules:
            return Verdict(action="allow")

        sink = self._sink

        async def _direct_publisher(
            _subject: str, event: dict[str, Any]
        ) -> None:
            # Orchestrator-side events bypass NATS — the subject is
            # ignored. Tenant/coworker values in the event came from
            # the SafetyContext we built server-side, so they are
            # already trusted; no second validation pass needed.
            audit = AuditEvent(
                tenant_id=str(event["tenant_id"]),
                coworker_id=event.get("coworker_id"),
                conversation_id=event.get("conversation_id"),
                job_id=event.get("job_id"),
                user_id=event.get("user_id"),
                stage=str(event["stage"]),
                verdict_action=str(event["verdict_action"]),
                triggered_rule_ids=list(event.get("triggered_rule_ids") or []),
                findings=list(event.get("findings") or []),
                context_digest=str(event.get("context_digest", "")),
                context_summary=str(event.get("context_summary", "")),
            )
            try:
                await sink.write(audit)
            except Exception as exc:  # noqa: BLE001 — audit must not break pipeline
                logger.error(
                    "safety: orchestrator audit write failed",
                    component="safety",
                    tenant_id=audit.tenant_id,
                    stage=audit.stage,
                    verdict=audit.verdict_action,
                    error=str(exc),
                )

        return await pipeline_run(
            rules, get_orchestrator_registry(), ctx, _direct_publisher
        )


__all__ = ["SafetyEngine"]
