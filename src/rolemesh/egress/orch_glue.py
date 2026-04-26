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
from .identity import IDENTITY_SNAPSHOT_SUBJECT, LIFECYCLE_SUBJECT  # noqa: E402
from .mcp_cache import (  # noqa: E402
    MCP_CHANGED_SUBJECT,
    MCP_SNAPSHOT_REQUEST_SUBJECT,
    McpEntry,
    entry_to_dict,
)
from .policy_cache import RULE_CHANGED_SUBJECT, SNAPSHOT_REQUEST_SUBJECT  # noqa: E402


# ---------------------------------------------------------------------------
# Identity registry (mirrors what we've published to the gateway)
# ---------------------------------------------------------------------------
#
# The gateway's identity cache is fed by two streams — an initial
# snapshot on its boot, then live lifecycle events. Before this module
# grew a registry, the snapshot was empty (returned []), so a gateway
# restart while agents were already running stranded every one of them
# at "Unknown source identity" until the next spawn.
#
# We mirror the same writes we send over NATS here so the snapshot
# responder has an authoritative view without having to re-walk Docker
# or cross-reference OrchestratorState. Because this registry and the
# NATS publish are updated atomically (same function body), the two
# cannot disagree: the gateway either sees an entry as both a snapshot
# entry and via a live 'started' event (idempotent on the gateway
# side), or sees neither.

_identity_registry: dict[str, dict[str, str]] = {}


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
    entry: dict[str, str] = {
        "container_name": container_name,
        "ip": ip,
        "tenant_id": tenant_id,
        "coworker_id": coworker_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "job_id": job_id,
    }
    # Register BEFORE publish so if the gateway happens to restart
    # between our publish and its subscription, the next snapshot still
    # includes this agent. Docker reuses bridge IPs only after the prior
    # container is removed, which in our flow comes with its own stop
    # event; see handle_stopped on the gateway side for the dedup.
    _identity_registry[container_name] = entry

    payload = {"event": "started", **entry}
    try:
        await nc.publish(LIFECYCLE_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("lifecycle publish failed (started)", error=str(exc))


async def publish_lifecycle_stopped(
    nc: nats.aio.client.Client,
    *,
    container_name: str,
) -> None:
    # Drop from the local registry first (mirrors the publish-before-
    # register ordering in publish_lifecycle_started). A benign race
    # where two stop events arrive back-to-back is harmless — the
    # second pop silently no-ops.
    _identity_registry.pop(container_name, None)

    payload = {"event": "stopped", "container_name": container_name}
    try:
        await nc.publish(LIFECYCLE_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("lifecycle publish failed (stopped)", error=str(exc))


async def publish_mcp_registry_changed(
    nc: nats.aio.client.Client,
    *,
    action: str,
    entry: McpEntry | None = None,
    name: str | None = None,
) -> None:
    """Push a single MCP registry delta to the gateway.

    For ``created`` / ``updated``: pass ``entry`` with the full payload.
    For ``deleted``: pass ``name`` (the only field the consumer needs).

    Best-effort publish: a failure here means the gateway misses one
    delta, but its next snapshot fetch on restart still recovers full
    state.
    """
    if action == "deleted":
        if not name:
            logger.warning("mcp_registry publish: deleted without name")
            return
        payload: dict[str, Any] = {"action": "deleted", "name": name}
    else:
        if entry is None:
            logger.warning(
                "mcp_registry publish: missing entry",
                action=action,
            )
            return
        payload = {"action": action, **entry_to_dict(entry)}
    try:
        await nc.publish(MCP_CHANGED_SUBJECT, json.dumps(payload).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "mcp_registry publish failed",
            error=str(exc),
            action=action,
        )


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
    mcp_fetcher: Callable[[], Awaitable[list[McpEntry]]] | None = None,
) -> list[object]:
    """Subscribe to all snapshot-request subjects and return sub handles.

    Caller is responsible for ``await sub.unsubscribe()`` at shutdown.
    Kept fire-and-forget rather than using JetStream: these are
    request-reply RPCs, so a missed reply surfaces as a gateway timeout
    and the gateway retries at its own cadence.

    ``rules_fetcher`` is injected so tests can feed a canned list
    without standing up the DB. Production wires it to
    ``fetch_all_egress_rules`` below.

    ``mcp_fetcher`` defaults to ``fetch_all_mcp_servers``, which
    reads the orchestrator's process-local ``_mcp_registry``. Tests
    can pass a stub. ``None`` (default) wires the production fetcher
    rather than skipping the responder, because a missing responder
    would cause every gateway boot to time out.
    """
    if mcp_fetcher is None:
        mcp_fetcher = fetch_all_mcp_servers

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

    async def _mcp_handler(msg: object) -> None:
        try:
            entries = await mcp_fetcher()
            body = json.dumps(
                {"entries": [entry_to_dict(e) for e in entries]}
            ).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.error("mcp snapshot fetch failed", error=str(exc))
            body = json.dumps({"entries": [], "error": str(exc)}).encode("utf-8")
        with contextlib.suppress(Exception):
            await msg.respond(body)  # type: ignore[attr-defined]

    rules_sub = await nc.subscribe(SNAPSHOT_REQUEST_SUBJECT, cb=_rules_handler)
    identity_sub = await nc.subscribe(IDENTITY_SNAPSHOT_SUBJECT, cb=_identity_handler)
    mcp_sub = await nc.subscribe(MCP_SNAPSHOT_REQUEST_SUBJECT, cb=_mcp_handler)
    logger.info(
        "egress responders subscribed",
        subjects=[
            SNAPSHOT_REQUEST_SUBJECT,
            IDENTITY_SNAPSHOT_SUBJECT,
            MCP_SNAPSHOT_REQUEST_SUBJECT,
        ],
    )
    return [rules_sub, identity_sub, mcp_sub]


async def fetch_all_mcp_servers() -> list[McpEntry]:
    """Production MCP fetcher: snapshot the orchestrator's in-process
    ``_mcp_registry``.

    The orchestrator already populates this dict in ``main`` by walking
    ``coworker.tools`` at startup. We read the dict directly rather than
    re-walking the DB because the dict is the authoritative
    "what has the orchestrator registered" view that future hot-reload
    publishers will keep in lockstep.
    """
    from rolemesh.egress.reverse_proxy import get_mcp_registry

    out: list[McpEntry] = []
    for name, (url, headers, auth_mode) in get_mcp_registry().items():
        out.append(
            McpEntry(
                name=name,
                url=url,
                headers=dict(headers),
                auth_mode=auth_mode,
            )
        )
    return out


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
    """Return all agents the orchestrator has registered as started.

    Source of truth is ``_identity_registry``, which the lifecycle
    publishers update in lockstep with their NATS publish. That means
    the snapshot mirrors the sum of every 'started' event we've sent
    minus every 'stopped' event — the same state the gateway would
    have assembled if it had been listening the whole time.

    ``state`` is unused now but kept in the signature so a future
    implementation can cross-check against OrchestratorState if we
    ever want a defense-in-depth consistency probe.
    """
    _ = state
    # Copy each entry so a concurrent publish mutating the registry
    # dict can't surprise JSON serialization.
    return [dict(entry) for entry in _identity_registry.values()]
