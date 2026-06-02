"""NATS transport for Orchestrator-side IPC.

Manages JetStream streams and KV buckets used for all 6 IPC channels
between the Orchestrator and container Agents.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nats
from nats.js.api import KeyValueConfig, StreamConfig

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from nats.aio.client import Client
    from nats.js.client import JetStreamContext

logger = get_logger()

# Stream max age: 1 hour in seconds (nats-py converts to nanoseconds internally)
_STREAM_MAX_AGE_S = 3600.0

# KV TTL: 1 hour in seconds
_KV_TTL_SECONDS = 3600.0

# Bound the INITIAL connect so a missing NATS fails boot fast with a helpful
# error, instead of blocking forever under max_reconnect_attempts=-1. Steady-
# state reconnects (after a connection has been established) stay unbounded.
_CONNECT_TIMEOUT_S = 10.0


class NatsTransport:
    """NATS transport for Orchestrator-side IPC.

    Provides JetStream and KV access after connect().
    """

    def __init__(self, url: str = "nats://localhost:4222") -> None:
        self._url = url
        self._nc: Client | None = None
        self._js: JetStreamContext | None = None

    async def connect(self) -> None:
        """Connect to NATS and create JetStream stream + KV buckets.

        Raises ConnectionError if NATS is unreachable.
        """

        async def _on_error(exc: Exception) -> None:
            # Per-attempt errors stay at DEBUG: with infinite reconnect this
            # fires on every retry during an outage, so a higher level would
            # spam. The once-per-event callbacks below carry the visible signal.
            logger.debug("NATS connection error", error=str(exc))

        async def _on_disconnected() -> None:
            logger.warning("NATS disconnected — reconnecting", url=self._url)

        async def _on_reconnected() -> None:
            # Durable consumers persist server-side across a NATS restart, so
            # push subscriptions resume on reconnect with no resubscribe needed.
            logger.info("NATS reconnected", url=self._url)

        try:
            # Retry reconnects forever. The previous 3-attempt budget (~3s)
            # meant any NATS outage longer than that exhausted the attempts and
            # nats-py permanently closed the connection — the orchestrator kept
            # running but was silently deaf to NATS until restarted.
            #
            # max_reconnect_attempts governs the INITIAL connect too, so under
            # -1 a missing NATS would block boot forever. Bound only the first
            # connect with wait_for to keep the fail-fast ConnectionError below;
            # once connected, reconnects are unbounded.
            self._nc = await asyncio.wait_for(
                nats.connect(
                    self._url,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=2,
                    error_cb=_on_error,
                    disconnected_cb=_on_disconnected,
                    reconnected_cb=_on_reconnected,
                ),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except Exception as exc:  # includes asyncio.TimeoutError (initial connect)
            raise ConnectionError(
                f"Cannot connect to NATS at {self._url}. "
                f"Start NATS with: docker compose -f docker-compose.dev.yml up -d"
            ) from exc
        self._js = self._nc.jetstream()

        # Create JetStream stream for agent communication.
        # Uses LIMITS retention (default) — WorkQueue doesn't allow multiple
        # consumers with overlapping subject filters, which we need since both
        # orchestrator and agent subscribe to different subjects in this stream.
        # `agent.*.interrupt` moved to JetStream so the Stop button works even
        # when Pi's event loop is under heavy stream-processing load. With core
        # NATS callback subscriptions, the interrupt SUB could race with the
        # orchestrator's publish and raise NoRespondersError; JetStream stores
        # the message and delivers once the consumer is ready.
        agent_stream = StreamConfig(
            name="agent-ipc",
            subjects=[
                "agent.*.results",
                "agent.*.input",
                "agent.*.interrupt",
                "agent.*.messages",
                "agent.*.tasks",
                # Safety Framework audit events — fire-and-forget
                # publishes from container; orchestrator-side subscriber
                # writes them to safety_decisions after a trusted-tenant
                # lookup. Lives on the same stream as other agent.*.*
                # subjects because its retention profile matches (short
                # buffer, consumer-driven drain).
                "agent.*.safety_events",
                # HITL approval IPC (docs/21-hitl-approval-plan.md §3). All
                # three subjects ride JetStream: the container publishes
                # approval_request / approval_cancel and js-subscribes the
                # decision; the orchestrator js-subscribes request/cancel and
                # publishes the decision. They must be in this stream or
                # ``js.subscribe`` raises NotFoundError at orchestrator startup.
                "agent.*.approval_request",
                "agent.*.approval_decision",
                "agent.*.approval_cancel",
            ],
            max_age=_STREAM_MAX_AGE_S,
        )
        try:
            await self._js.add_stream(agent_stream)
        except Exception:
            # Stream already exists with different config (e.g. older deploy
            # didn't include agent.*.interrupt) — update it instead of failing.
            await self._js.update_stream(agent_stream)

        # Create KV buckets
        await self._js.create_key_value(config=KeyValueConfig(bucket="agent-init", ttl=_KV_TTL_SECONDS))
        await self._js.create_key_value(config=KeyValueConfig(bucket="snapshots", ttl=_KV_TTL_SECONDS))

        # Create JetStream stream for web channel communication.
        await self._js.add_stream(
            StreamConfig(
                name="web-ipc",
                subjects=["web.>"],
                max_age=_STREAM_MAX_AGE_S,
            )
        )

        logger.info("NATS connected", url=self._url)

    @property
    def nc(self) -> Client:
        """Return the raw NATS client. Raises if not connected."""
        assert self._nc is not None, "NatsTransport not connected"
        return self._nc

    @property
    def js(self) -> JetStreamContext:
        """Return the JetStream context. Raises if not connected."""
        assert self._js is not None, "NatsTransport not connected"
        return self._js

    async def close(self) -> None:
        """Close the NATS connection."""
        if self._nc:
            await self._nc.close()
            self._nc = None
            self._js = None
            logger.info("NATS connection closed")
