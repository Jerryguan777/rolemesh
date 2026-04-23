"""In-gateway cache of egress-scope safety rules (EC-2).

The gateway's hot path runs ``get_rules_for(tenant_id, coworker_id)`` on
every allowed-or-blocked decision. Going to PostgreSQL each time would
add a round trip that the forward proxy and DNS resolver can't afford,
so the cache is the single source of truth at request time.

Rules the cache holds are limited to ``stage == 'egress_request'``:
every other stage lives inside agent containers (hook handler) or the
orchestrator (MODEL_OUTPUT pipeline), so the gateway would never need
them.

Lifecycle
---------

    startup                    hot reload
    ┌───────┐                  ┌─────────────────────┐
    │ NATS  │                  │ NATS                │
    │ rpc   │                  │ safety.rule.changed │
    │ snap  │                  │                     │
    └───┬───┘                  └──────────┬──────────┘
        │                                 │
        ▼                                 ▼
    PolicyCache.seed(...)            PolicyCache.apply_event(...)
        │                                 │
        └─────────────┬───────────────────┘
                      ▼
              get_rules_for(tid, cwid)

Seed is fail-closed: a gateway that can't read the current rule set
refuses to start. The runtime update path is fail-safe (a malformed
event is logged and dropped) — one bad event must not crash the
gateway or it becomes a denial-of-service vector.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    import nats.aio.client

logger = get_logger()

# NATS subjects owned by the egress control loop. Orchestrator REST
# handlers publish on RULE_CHANGED_SUBJECT after every successful
# safety_rules CRUD. Gateway requests the initial snapshot via
# SNAPSHOT_REQUEST_SUBJECT (core NATS request-reply).
RULE_CHANGED_SUBJECT = "safety.rule.changed"
SNAPSHOT_REQUEST_SUBJECT = "egress.rules.snapshot.request"


@dataclass(frozen=True)
class CachedRule:
    """Pared-down view of ``SafetyRule``.

    We deliberately do not import ``rolemesh.safety.types.Rule`` — the
    gateway module should not depend on the richer dataclass hierarchy,
    and the subset we care about fits in plain dicts. ``config`` is
    kept as ``dict[str, Any]`` so the check can inspect its own schema
    without another conversion step.
    """

    id: str
    tenant_id: str
    coworker_id: str | None
    stage: str
    check_id: str
    config: dict[str, Any]
    priority: int
    enabled: bool


@dataclass
class PolicyCache:
    """Two-level index keyed by ``(tenant_id, coworker_id)``.

    ``None`` in the inner key means "tenant-wide" (applies to every
    coworker in the tenant). ``get_rules_for`` merges the coworker-
    specific rules with the tenant-wide rules at lookup time. Keeping
    them in separate buckets at cache level matches the DB row shape
    one-to-one and makes invalidation straightforward.
    """

    _rules: dict[str, dict[str | None, list[CachedRule]]] = field(default_factory=dict)
    _by_id: dict[str, CachedRule] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _insert_locked(self, rule: CachedRule) -> None:
        """Caller MUST hold ``_lock``. Separated so apply_event can
        chain insert-after-delete without re-acquiring."""
        tenant_bucket = self._rules.setdefault(rule.tenant_id, {})
        coworker_bucket = tenant_bucket.setdefault(rule.coworker_id, [])
        # Replace in place if the same id already existed — keeps
        # ordering deterministic (insertion order) for callers that
        # care about priority ties.
        for i, existing in enumerate(coworker_bucket):
            if existing.id == rule.id:
                coworker_bucket[i] = rule
                break
        else:
            coworker_bucket.append(rule)
        self._by_id[rule.id] = rule

    def _remove_locked(self, rule_id: str) -> None:
        rule = self._by_id.pop(rule_id, None)
        if rule is None:
            return
        tenant_bucket = self._rules.get(rule.tenant_id)
        if tenant_bucket is None:
            return
        coworker_bucket = tenant_bucket.get(rule.coworker_id)
        if coworker_bucket is None:
            return
        coworker_bucket[:] = [r for r in coworker_bucket if r.id != rule_id]

    async def seed(self, snapshot: Iterable[dict[str, Any]]) -> None:
        """Replace the entire cache contents from a snapshot.

        Used once at startup; the lock is held for the whole replacement
        so no hot-path lookup can observe a half-populated cache.
        """
        async with self._lock:
            self._rules = {}
            self._by_id = {}
            count = 0
            for entry in snapshot:
                try:
                    rule = _rule_from_dict(entry)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("policy_cache: skipping malformed rule", error=str(exc))
                    continue
                if not rule.enabled:
                    continue
                self._insert_locked(rule)
                count += 1
        logger.info("policy_cache: seeded", rule_count=count)

    async def apply_event(self, event: dict[str, Any]) -> None:
        """Update the cache in response to a ``safety.rule.changed`` event.

        Event shape:
            {
              "action": "created" | "updated" | "deleted",
              "rule_id": "<uuid>",
              "tenant_id": "<uuid>",
              "coworker_id": "<uuid>" | null,
              "stage": "egress_request",
              "check_id": "egress.domain_rule",
              "enabled": true,
              "config": {...},
              "priority": 100,
            }
        Non-egress-stage events are dropped silently — those update
        state that does not live in this cache.
        """
        action = event.get("action")
        if event.get("stage") != "egress_request" and action != "deleted":
            # A stage change (updated to/from egress_request) reaches us
            # as an "updated" event; we only filter on non-deleted
            # events so a rule that moved OUT of egress_request is
            # still removed below.
            return

        rule_id = str(event.get("rule_id", ""))
        if not rule_id:
            logger.warning("policy_cache: event missing rule_id", payload=event)
            return

        async with self._lock:
            if action == "deleted" or not event.get("enabled", True):
                self._remove_locked(rule_id)
                return
            if action not in ("created", "updated"):
                logger.warning("policy_cache: unknown action", action=action)
                return
            try:
                rule = _rule_from_dict(event)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "policy_cache: malformed event — dropping",
                    error=str(exc),
                    rule_id=rule_id,
                )
                return
            self._insert_locked(rule)
        logger.debug(
            "policy_cache: applied event",
            action=action,
            rule_id=rule_id,
        )

    def get_rules_for(self, tenant_id: str, coworker_id: str) -> list[CachedRule]:
        """Return rules that apply to a specific coworker, tenant-first.

        Merges tenant-wide (``coworker_id is None``) with coworker-
        specific rules. Sort by descending priority so higher-priority
        rules are evaluated first (matches the orchestrator pipeline's
        rule ordering convention).
        """
        tenant_bucket = self._rules.get(tenant_id, {})
        out: list[CachedRule] = []
        out.extend(tenant_bucket.get(None, []))
        out.extend(tenant_bucket.get(coworker_id, []))
        out.sort(key=lambda r: r.priority, reverse=True)
        return out

    def __len__(self) -> int:
        return len(self._by_id)


def _rule_from_dict(d: dict[str, Any]) -> CachedRule:
    """Build a CachedRule from the flat-dict shape used on the wire.

    The orchestrator publishes events with a consistent snake_case key
    set; the DB snapshot path uses the same ``to_snapshot_dict`` output.
    Keeping a single constructor means malformed events fail here, once,
    rather than inside every caller.
    """
    coworker_id = d.get("coworker_id")
    return CachedRule(
        id=str(d["rule_id"]) if "rule_id" in d else str(d["id"]),
        tenant_id=str(d["tenant_id"]),
        coworker_id=str(coworker_id) if coworker_id else None,
        stage=str(d.get("stage", "egress_request")),
        check_id=str(d["check_id"]),
        config=dict(d.get("config", {})),
        priority=int(d.get("priority", 100)),
        enabled=bool(d.get("enabled", True)),
    )


async def fetch_snapshot_via_nats(
    nats_client: nats.aio.client.Client,
    *,
    timeout_s: float = 5.0,
) -> list[dict[str, Any]]:
    """Request the current egress-rule snapshot from the orchestrator.

    Wraps a core NATS request-reply on ``SNAPSHOT_REQUEST_SUBJECT``.
    Using core NATS (not JetStream) because the snapshot is a one-shot
    operation; a missed reply should surface as a timeout here, not be
    persisted and replayed.

    Raises ``TimeoutError`` or whatever the NATS client raises on
    transport failure. The launcher is expected to fail-close on
    exception.
    """
    response = await nats_client.request(  # type: ignore[attr-defined]
        SNAPSHOT_REQUEST_SUBJECT,
        b"",
        timeout=timeout_s,
    )
    payload = json.loads(response.data)
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise ValueError(
            f"Unexpected snapshot shape: expected list under 'rules', got {type(rules).__name__}"
        )
    return rules


async def subscribe_rule_changes(
    nats_client: nats.aio.client.Client,
    cache: PolicyCache,
) -> object:
    """Subscribe to RULE_CHANGED_SUBJECT and route events to *cache*."""
    async def _handler(msg: object) -> None:
        try:
            event = json.loads(msg.data)  # type: ignore[attr-defined]
        except (ValueError, AttributeError) as exc:
            logger.warning("policy_cache: non-JSON rule-change event", error=str(exc))
            return
        await cache.apply_event(event)

    sub = await nats_client.subscribe(RULE_CHANGED_SUBJECT, cb=_handler)  # type: ignore[attr-defined]
    logger.info("policy_cache: subscribed", subject=RULE_CHANGED_SUBJECT)
    return sub
