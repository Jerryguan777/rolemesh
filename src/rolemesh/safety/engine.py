"""Orchestrator-side SafetyEngine.

Thin façade that the WebUI admin REST layer and the NATS
``safety_events`` subscriber both depend on:

  - ``load_rules_for_coworker`` — snapshot the ``safety_rules`` rows
    for admin-side / API inspection. Raises on DB failure so callers
    see the error rather than silently receiving an empty list.
    Container startup does NOT use this path — it uses
    ``rolemesh.safety.loader.load_safety_rules_snapshot``, which
    layers ``SAFETY_FAIL_MODE`` dispatch on top of the same query.
  - ``handle_safety_event`` — decode a NATS payload into an
    ``AuditEvent`` and forward it to the configured sink.

Both rule-reading paths share ``fetch_safety_rule_snapshots`` in
loader.py so the query + serialization live in one place; only the
error handling differs.

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
