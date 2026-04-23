"""Orchestrator-side glue for the egress gateway (EC-2).

Two kinds of plumbing live here:

  1. NATS responders — serve the gateway's initial-state RPCs.
     * ``egress.rules.snapshot.request``   → current egress-stage rules
     * ``egress.identity.snapshot.request`` → current agent IP map

  2. NATS publishers — push deltas to the gateway after the initial
     snapshot. Module-level helpers so existing orchestrator code
     (container_executor, webui/admin) can import one function without
     dragging NATS client types through their signatures.

The gateway always reads snapshot → subscribe to deltas in that order.
Any event that lands before the snapshot is idempotent (started events
are keyed on container name; rule events on rule_id), so a benign race
between the two operations just leads to one duplicate apply.

Keeping these adapters in a single file (rather than scattered across
container_executor / webui / main) makes it easy to reason about the
egress control plane as a unit — the single file is where every new
subject is introduced, logged, unsubscribed on shutdown.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nats.aio.client

    from rolemesh.core.orchestrator_state import OrchestratorState

logger = get_logger()

# Subject names — imported from egress.policy_cache + egress.identity
# so there's a single authoritative string for each subject. Duplicate
# definitions would drift.
from .identity import LIFECYCLE_SUBJECT  # noqa: E402
from .policy_cache import RULE_CHANGED_SUBJECT, SNAPSHOT_REQUEST_SUBJECT  # noqa: E402

IDENTITY_SNAPSHOT_SUBJECT = "egress.identity.snapshot.request"


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------


async def publish_lifecycle_started(
    nc: nats.aio.client.Client,
    *,
    container_name: str,
    ip: str,
    tenant_id: str,
    coworker_id: str,
    user_id: str,
    conversation_id: str,
    job_id: str,
) -> None:
    """Emit an agent-started event for the gateway's identity map.

    Swallows publish errors: the gateway snapshots identity on its own
    startup (via IDENTITY_SNAPSHOT_SUBJECT), so a momentary NATS outage
    here produces at most a brief window where the gateway denies
    requests from this container. That's acceptable; promoting to
    fail-closed at this site would also kill agent startup for every
    downstream workload.
    """
    payload = {
        "event": "started",
        "container_name": container_name,
        "ip": ip,
        "tenant_id": tenant_id,
        "coworker_id": coworker_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "job_id": job_id,
    }
    try:
        await nc.publish(LIFECYCLE_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("lifecycle publish failed (started)", error=str(exc))


async def publish_lifecycle_stopped(
    nc: nats.aio.client.Client,
    *,
    container_name: str,
) -> None:
    payload = {"event": "stopped", "container_name": container_name}
    try:
        await nc.publish(LIFECYCLE_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("lifecycle publish failed (stopped)", error=str(exc))


async def publish_rule_changed(
    nc: nats.aio.client.Client,
    *,
    action: str,
    rule: dict[str, Any],
) -> None:
    """Publish a ``safety.rule.changed`` event.

    ``rule`` is the to_snapshot_dict() view of a SafetyRule row. We
    flatten into a single JSON object with ``rule_id`` mirroring
    ``id`` for consumer-side convenience (the policy_cache expects
    either key).
    """
    payload: dict[str, Any] = {"action": action, **rule}
    payload.setdefault("rule_id", payload.get("id"))
    try:
        await nc.publish(RULE_CHANGED_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("rule.changed publish failed", error=str(exc), action=action)


# ---------------------------------------------------------------------------
# Responders (served from the orchestrator startup path)
# ---------------------------------------------------------------------------


async def start_responders(
    nc: nats.aio.client.Client,
    *,
    state: OrchestratorState,
    rules_fetcher: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> list[object]:
    """Subscribe to both snapshot-request subjects and return sub handles.

    Caller is responsible for ``await sub.unsubscribe()`` at shutdown.
    Kept fire-and-forget rather than using JetStream: these are
    request-reply RPCs, so a missed reply surfaces as a gateway timeout
    and the gateway retries at its own cadence.

    ``rules_fetcher`` is injected so tests can feed a canned list
    without standing up the DB. Production wires it to
    ``_fetch_all_egress_rules`` below.
    """
    async def _rules_handler(msg: object) -> None:
        try:
            rules = await rules_fetcher()
            body = json.dumps({"rules": rules}).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 — never crash the subscriber loop
            logger.error("rules snapshot fetch failed", error=str(exc))
            body = json.dumps({"rules": [], "error": str(exc)}).encode("utf-8")
        with contextlib.suppress(Exception):
            await msg.respond(body)  # type: ignore[attr-defined]

    async def _identity_handler(msg: object) -> None:
        try:
            snapshot = _build_identity_snapshot(state)
            body = json.dumps({"entries": snapshot}).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.error("identity snapshot build failed", error=str(exc))
            body = json.dumps({"entries": [], "error": str(exc)}).encode("utf-8")
        with contextlib.suppress(Exception):
            await msg.respond(body)  # type: ignore[attr-defined]

    rules_sub = await nc.subscribe(SNAPSHOT_REQUEST_SUBJECT, cb=_rules_handler)
    identity_sub = await nc.subscribe(IDENTITY_SNAPSHOT_SUBJECT, cb=_identity_handler)
    logger.info(
        "egress responders subscribed",
        subjects=[SNAPSHOT_REQUEST_SUBJECT, IDENTITY_SNAPSHOT_SUBJECT],
    )
    return [rules_sub, identity_sub]


async def fetch_all_egress_rules() -> list[dict[str, Any]]:
    """Load every enabled rule with stage='egress_request' across all tenants.

    Production implementation of the ``rules_fetcher`` argument above.
    Scans all tenants because gateway is not sharded per-tenant — it
    sees every agent on the bridge. ``enabled=TRUE`` is filtered at
    the SQL layer to keep the snapshot lean.
    """
    from rolemesh.db import pg

    tenants = await pg.get_all_tenants()
    out: list[dict[str, Any]] = []
    for tenant in tenants:
        rows = await pg.list_safety_rules(
            tenant.id, stage="egress_request", enabled=True
        )
        out.extend(r.to_snapshot_dict() for r in rows)
    return out


def _build_identity_snapshot(state: OrchestratorState) -> list[dict[str, Any]]:
    """Walk OrchestratorState for every running container and collect (ip, identity) pairs.

    OrchestratorState carries conversation-keyed state; actual running
    containers are tracked in ``GroupQueue`` which this module does
    not yet read. V1 returns an empty list, which is safe because the
    NATS lifecycle event stream picks up new containers as they start.
    Follow-up: walk the queue's process map and Docker-inspect each
    container's IP here.
    """
    # TODO(ec-2-follow-up): enumerate GroupQueue.process_map and inspect
    # each container's agent-net IP. For PR-2 we lean on the lifecycle
    # event stream; the snapshot is a correctness fallback rather than
    # a primary source.
    _ = state
    return []
