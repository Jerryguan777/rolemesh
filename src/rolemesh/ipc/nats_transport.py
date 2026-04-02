"""NATS transport for Orchestrator-side IPC.

Manages JetStream streams and KV buckets used for all 6 IPC channels
between the Orchestrator and container Agents.
"""

from __future__ import annotations

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

        async def _quiet_error(exc: Exception) -> None:
            logger.debug("NATS connection attempt failed", error=str(exc))

        try:
            self._nc = await nats.connect(
                self._url,
                max_reconnect_attempts=3,
                reconnect_time_wait=1,
                error_cb=_quiet_error,
            )
        except Exception as exc:
            raise ConnectionError(
                f"Cannot connect to NATS at {self._url}. "
                f"Start NATS with: docker compose -f docker-compose.dev.yml up -d"
            ) from exc
        self._js = self._nc.jetstream()

        # Create JetStream stream for agent communication.
        # Uses LIMITS retention (default) — WorkQueue doesn't allow multiple
        # consumers with overlapping subject filters, which we need since both
        # orchestrator and agent subscribe to different subjects in this stream.
        await self._js.add_stream(
            StreamConfig(
                name="agent-ipc",
                subjects=["agent.*.results", "agent.*.input", "agent.*.messages", "agent.*.tasks"],
                max_age=_STREAM_MAX_AGE_S,
            )
        )

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
