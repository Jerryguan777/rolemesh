"""Audit sink for safety decisions.

The orchestrator receives ``safety_events`` NATS messages from
containers and writes them here. The sink deliberately persists only a
payload digest + short summary — not the original text — so the audit
table cannot double as a PII leak vector (see design §5.10).

``DbAuditSink`` is the production implementation backed by
``rolemesh.db``. Tests use their own in-memory fake.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from rolemesh.db import (
    insert_safety_decision,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class AuditEvent:
    """Decoded safety_events message.

    ``triggered_rule_ids`` lists the rules whose verdict materially
    shaped the final decision — for a single-rule block, that's one id;
    for a redact chain, all rules that produced modifications.

    ``approval_context`` is retained only for ``verdict_action='require_approval'``
    rows at the PRE_TOOL_CALL stage. Deliberately the ONLY place the
    full tool_input lives after the turn — the rest of the audit
    table keeps only a digest + short summary to avoid becoming a PII
    sink. See the ``safety_decisions`` schema note about the 24h
    cleanup policy for this column.
    """

    tenant_id: str
    coworker_id: str | None
    conversation_id: str | None
    job_id: str | None
    # V2 P1.1: carried so the require_approval bridge can attribute
    # the approval_request.user_id to the user whose turn triggered
    # the safety gate. Not persisted in safety_decisions (would
    # duplicate data the audit table doesn't need) — this field is
    # in-flight metadata only.
    user_id: str | None
    stage: str
    verdict_action: str
    triggered_rule_ids: list[str]
    findings: list[dict[str, Any]]
    context_digest: str
    context_summary: str
    approval_context: dict[str, Any] | None = None


class AuditSink(Protocol):
    async def write(self, event: AuditEvent) -> None: ...


class DbAuditSink:
    """Persists AuditEvent to the ``safety_decisions`` table."""

    async def write(self, event: AuditEvent) -> None:

        await insert_safety_decision(
            tenant_id=event.tenant_id,
            coworker_id=event.coworker_id,
            conversation_id=event.conversation_id,
            job_id=event.job_id,
            stage=event.stage,
            verdict_action=event.verdict_action,
            triggered_rule_ids=event.triggered_rule_ids,
            findings=event.findings,
            context_digest=event.context_digest,
            context_summary=event.context_summary,
            approval_context=event.approval_context,
        )


def compute_context_digest(payload: Mapping[str, Any]) -> str:
    """Stable SHA-256 of a payload dict.

    ``sort_keys=True`` so a dict reordering does not produce a new
    digest. Audit queries key off this value to deduplicate repeated
    identical blocks (e.g. retry loops).
    """
    # json.dumps accepts Mapping; the dict() coerce is only needed on
    # CPython <=3.12 stubs that over-narrow it. Using dict() keeps
    # static typers happy without runtime cost.
    data = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(data).hexdigest()


def summarize_context(stage: str, payload: Mapping[str, Any]) -> str:
    """Short human-readable summary. Max ~80 chars to keep the table compact."""
    if stage == "pre_tool_call":
        tool = payload.get("tool_name", "?")
        return f"tool={tool}"
    if stage == "input_prompt":
        prompt = str(payload.get("prompt", ""))
        return f"prompt={prompt[:40]}"
    if stage == "model_output":
        text = str(payload.get("text", ""))
        return f"output={text[:40]}"
    if stage == "post_tool_result":
        tool = payload.get("tool_name", "?")
        return f"tool_result={tool}"
    if stage == "pre_compaction":
        return "compaction"
    return stage


__all__ = [
    "AuditEvent",
    "AuditSink",
    "DbAuditSink",
    "compute_context_digest",
    "summarize_context",
]
