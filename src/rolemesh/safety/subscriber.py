"""NATS subscriber that ingests ``agent.*.safety_events`` into the audit sink.

Previously (V1 initial): ``SafetyEngine.handle_safety_event`` trusted
the payload's claimed ``tenant_id`` / ``coworker_id`` at face value.
That would have let a buggy or malicious container poison another
tenant's safety_decisions table — a real multi-tenant isolation
bug. The subscriber now sits between NATS and the engine, performing
the same authoritative lookup that ``rolemesh/main.py _handle_tasks``
performs for IPC tasks: the claimed identifiers in the payload are
cross-checked against the orchestrator's in-memory coworker map, and
any mismatch is dropped with a loud WARNING.

Separation of concerns:

  - ``SafetyEventsSubscriber`` — transport + trust boundary
  - ``SafetyEngine.handle_safety_event`` — pure audit processor,
    sees only already-validated payloads

This lets the subscriber unit test focus on trust-check edges, and
the engine test focus on sink behaviour, without overlap.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from .engine import SafetyEngine

logger = get_logger()


class TrustedCoworker(Protocol):
    """What the subscriber needs from an orchestrator coworker record.

    Kept minimal so the subscriber does not pick up a dependency on
    OrchestratorState; unit tests pass a simple lookup callable
    without standing up the full state machine.
    """

    tenant_id: str
    id: str


class SafetyEventsSubscriber:
    """Consumes NATS ``agent.*.safety_events`` and forwards validated
    events to the SafetyEngine.

    The subscriber is intentionally non-async in its validation path —
    the trust check uses only the in-memory coworker map so it cannot
    stall on I/O. Only the downstream ``engine.handle_safety_event``
    call reaches the DB.
    """

    def __init__(
        self,
        *,
        engine: SafetyEngine,
        coworker_lookup: Callable[[str], TrustedCoworker | None],
    ) -> None:
        self._engine = engine
        self._lookup = coworker_lookup

    async def on_payload(self, payload: dict[str, Any]) -> None:
        """Process one decoded event payload.

        Public for two reasons: (1) testing — unit tests invoke this
        directly with synthetic payloads, bypassing NATS; (2) the
        orchestrator's NATS message loop also calls it after decoding
        the msg.data bytes, keeping the transport-vs-logic split
        clean.
        """
        claimed_tenant = str(payload.get("tenant_id") or "")
        claimed_coworker = str(payload.get("coworker_id") or "")

        if not claimed_coworker:
            logger.warning(
                "safety: dropping event with no coworker_id",
                component="safety",
                stage=payload.get("stage"),
            )
            return

        trusted = self._lookup(claimed_coworker)
        if trusted is None:
            logger.warning(
                "safety: dropping event — unknown coworker_id",
                component="safety",
                claimed_coworker=claimed_coworker,
                claimed_tenant=claimed_tenant,
            )
            return

        # Cross-check the claimed tenant_id against the authoritative
        # record. Mirrors _tenant_matches in approval/engine.py — a
        # buggy or malicious container must NOT be able to write audit
        # rows against another tenant.
        #
        # Note on ``claimed_tenant and ...`` — if the event omits
        # tenant_id entirely (empty string), this branch is skipped
        # and line below just overwrites with the trusted value.
        # That IS intentional: the ``coworker_id`` lookup above is
        # the real gate — once the coworker maps to a tenant in our
        # in-memory state, no cross-tenant leak is possible
        # regardless of what the claimed tenant says. The
        # non-empty-claim mismatch check is a defence-in-depth /
        # observability hook so a buggy container claiming the
        # wrong tenant leaves a breadcrumb in logs instead of
        # being silently "corrected".
        if claimed_tenant and claimed_tenant != trusted.tenant_id:
            logger.warning(
                "safety: dropping event — tenant_id mismatch",
                component="safety",
                claimed_tenant=claimed_tenant,
                trusted_tenant=trusted.tenant_id,
                coworker_id=claimed_coworker,
            )
            return

        # Rewrite payload with the authoritative tenant_id so the
        # engine never sees anything but validated data.
        validated = dict(payload)
        validated["tenant_id"] = trusted.tenant_id
        validated["coworker_id"] = trusted.id
        await self._engine.handle_safety_event(validated)

    async def on_message_bytes(self, data: bytes) -> None:
        """NATS transport entry point — decode JSON then validate.

        Malformed JSON is dropped with a log. Same fail-safe contract
        as approval: one bad message must not poison the subscriber
        loop.
        """
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "safety: dropping event — JSON decode failed",
                component="safety",
                error=str(exc),
            )
            return
        if not isinstance(payload, dict):
            logger.warning(
                "safety: dropping event — payload is not a JSON object",
                component="safety",
                type=type(payload).__name__,
            )
            return
        await self.on_payload(payload)


__all__ = ["SafetyEventsSubscriber", "TrustedCoworker"]
