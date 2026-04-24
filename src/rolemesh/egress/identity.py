"""Source-IP → agent identity lookup for the egress gateway (EC-2).

Every request the gateway accepts arrives from some IP on
``rolemesh-agent-net``. That IP is the container's bridge interface; we
map it back to the authoritative (tenant_id, coworker_id, user_id,
conversation_id, job_id) the orchestrator assigned when the container
started.

Data flow
---------

    orchestrator (ContainerAgentExecutor)
            │
            ▼  publishes on ``orchestrator.agent.lifecycle``
    NATS JetStream
            │
            ▼  durable consumer ``egress-identity``
    IdentityResolver   ← Gateway subscribes on startup
            │
            ▼  by_ip(source_ip) -> Identity | None
    Safety pipeline call

Fail-closed policy: an unknown source IP is NEVER mapped to a default
tenant. The pipeline call denies; the attacker sees 403 / NXDOMAIN.
Unknown IP is typically a race between "agent container bound IP" and
"orchestrator published lifecycle event", so a brief refuse-then-allow
window is acceptable — the alternative is a permanent security gap.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    import nats.aio.client

logger = get_logger()


# NATS subject published by the orchestrator whenever an agent container
# is started or stopped. Fixed name so the gateway's durable consumer
# name is also stable — a rename would silently orphan the subscription.
LIFECYCLE_SUBJECT = "orchestrator.agent.lifecycle"


@dataclass(frozen=True)
class Identity:
    """Authoritative view of which agent a request came from.

    Frozen so the gateway cannot accidentally mutate a shared record
    while it's in flight through the safety pipeline. All fields are
    strings because the downstream safety_decisions table keys off
    strings; asking Identity to know about UUID types would force an
    ``asyncpg`` import into every gateway code path.
    """

    tenant_id: str
    coworker_id: str
    user_id: str
    conversation_id: str
    job_id: str
    container_name: str


@dataclass
class IdentityResolver:
    """In-memory IP → Identity map, kept warm by NATS lifecycle events.

    The resolver owns two maps:

      - ``by_ip``        — the O(1) hot-path lookup used on every request
      - ``by_container`` — secondary index for stop events, which arrive
                           with a container name but not an IP

    Both are updated transactionally under ``_lock`` so a partial update
    can't expose a container whose IP was freed to a stale identity. The
    lock is contended only during start/stop events, not on the hot path.
    """

    by_ip: dict[str, Identity] = field(default_factory=dict)
    by_container: dict[str, Identity] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def handle_started(self, event: dict[str, object]) -> None:
        """Register a new agent container. Idempotent."""
        try:
            ip = str(event["ip"])
            identity = Identity(
                tenant_id=str(event["tenant_id"]),
                coworker_id=str(event["coworker_id"]),
                user_id=str(event.get("user_id", "")),
                conversation_id=str(event.get("conversation_id", "")),
                job_id=str(event.get("job_id", "")),
                container_name=str(event["container_name"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "identity: malformed started event — skipping",
                error=str(exc),
                payload=event,
            )
            return

        async with self._lock:
            # If the IP was previously bound to a different container
            # (rare: Docker recycled the IP before we got the stop
            # event), overwrite. Docker guarantees a given IP is only
            # bound to one container at a time.
            old = self.by_ip.pop(ip, None)
            if old is not None and old.container_name != identity.container_name:
                self.by_container.pop(old.container_name, None)

            self.by_ip[ip] = identity
            self.by_container[identity.container_name] = identity

        logger.info(
            "identity: agent registered",
            container=identity.container_name,
            ip=ip,
            tenant_id=identity.tenant_id,
        )

    async def handle_stopped(self, event: dict[str, object]) -> None:
        """Deregister an agent container. Idempotent."""
        container_name = str(event.get("container_name", ""))
        if not container_name:
            # ``payload=`` rather than ``event=`` — structlog reserves
            # ``event`` as the message slot; passing it as a kwarg
            # raises TypeError ("got multiple values for argument
            # 'event'") inside the warning call, which nats-py would
            # silently eat and the malformed event would disappear.
            logger.warning("identity: stopped event without container_name", payload=event)
            return

        async with self._lock:
            identity = self.by_container.pop(container_name, None)
            if identity is None:
                return
            # Remove from by_ip only if it still points at the same
            # container. If Docker already reassigned the IP to a new
            # agent, the started event for that new agent will have
            # written a fresh entry — we must not clobber it.
            for ip, cur in list(self.by_ip.items()):
                if cur.container_name == container_name:
                    del self.by_ip[ip]

        logger.info(
            "identity: agent deregistered",
            container=container_name,
        )

    async def seed(self, snapshots: Iterable[dict[str, object]]) -> None:
        """Bulk-load at startup before subscribing to lifecycle events.

        The gateway fetches the current-state snapshot from the
        orchestrator's ``/api/internal/egress/identity-snapshot`` endpoint
        and hands it here. Seeding happens BEFORE the NATS subscription
        opens so we don't race the first live event.
        """
        for entry in snapshots:
            await self.handle_started(entry)
        logger.info("identity: seeded", count=len(self.by_ip))

    def resolve(self, source_ip: str) -> Identity | None:
        """Hot-path lookup. Does not take the lock — single-word reads
        from a dict are safe under the GIL, and the occasional
        stale-miss is acceptable (see fail-closed policy in module
        docstring).
        """
        return self.by_ip.get(source_ip)


async def subscribe_lifecycle(
    nats_client: nats.aio.client.Client,
    resolver: IdentityResolver,
    *,
    durable: str = "egress-identity",
) -> object:
    """Subscribe to LIFECYCLE_SUBJECT and route events to *resolver*.

    Returns the subscription handle; caller is responsible for
    ``unsubscribe()`` at shutdown.

    Uses core NATS (not JetStream) because lifecycle events are
    idempotent and the snapshot-seed on startup covers any events lost
    during gateway downtime. Durable-consumer JetStream would add
    complexity for no correctness gain.
    """
    async def _handler(msg: object) -> None:
        try:
            payload = json.loads(msg.data)  # type: ignore[attr-defined]
        except (ValueError, AttributeError) as exc:
            logger.warning("identity: non-JSON lifecycle event", error=str(exc))
            return
        event_type = payload.get("event")
        if event_type == "started":
            await resolver.handle_started(payload)
        elif event_type == "stopped":
            await resolver.handle_stopped(payload)
        else:
            logger.warning("identity: unknown event type", event=event_type)

    sub = await nats_client.subscribe(  # type: ignore[attr-defined]
        LIFECYCLE_SUBJECT,
        queue=durable,
        cb=_handler,
    )
    logger.info("identity: subscribed to lifecycle events", subject=LIFECYCLE_SUBJECT)
    return sub
