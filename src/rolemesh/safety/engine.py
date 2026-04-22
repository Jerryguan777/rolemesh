"""Orchestrator-side SafetyEngine.

Thin façade that the WebUI admin REST layer and the NATS
``safety_events`` subscriber both depend on:

  - ``load_rules_for_coworker`` — snapshot the ``safety_rules`` rows
    the container will evaluate for a given coworker; called by
    ``container_executor`` at job start and by unit tests.
  - ``handle_safety_event`` — decode a NATS payload into an
    ``AuditEvent`` and forward it to the configured sink.

The engine deliberately holds no state; rule reloads are just fresh
queries. V2 adds ``handle_rpc_request`` for slow-check RPC.
"""

from __future__ import annotations

from typing import Any

from rolemesh.core.logger import get_logger

from .audit import AuditEvent, AuditSink, DbAuditSink

logger = get_logger()


class SafetyEngine:
    def __init__(self, *, audit_sink: AuditSink | None = None) -> None:
        self._sink: AuditSink = audit_sink or DbAuditSink()

    async def load_rules_for_coworker(
        self, tenant_id: str, coworker_id: str
    ) -> list[dict[str, Any]]:
        """Return the enabled rules for a coworker as container-ready dicts.

        Raises ``Exception`` on DB failure — callers decide between
        fail-open and fail-close. Matches the contract that
        ``get_enabled_policies_for_coworker`` exposes to the approval
        module.
        """
        from rolemesh.db.pg import list_safety_rules_for_coworker

        rules = await list_safety_rules_for_coworker(tenant_id, coworker_id)
        return [r.to_snapshot_dict() for r in rules if r.enabled]

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
            event = AuditEvent(
                tenant_id=str(payload["tenant_id"]),
                coworker_id=payload.get("coworker_id"),
                conversation_id=payload.get("conversation_id"),
                job_id=payload.get("job_id"),
                stage=str(payload["stage"]),
                verdict_action=str(payload["verdict_action"]),
                triggered_rule_ids=list(payload.get("triggered_rule_ids") or []),
                findings=list(payload.get("findings") or []),
                context_digest=str(payload.get("context_digest", "")),
                context_summary=str(payload.get("context_summary", "")),
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


__all__ = ["SafetyEngine"]
