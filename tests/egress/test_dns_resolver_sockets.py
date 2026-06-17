"""End-to-end socket test of the internal-name exemption.

Unlike test_dns_resolver.py (which stubs the forwarder methods), this binds
a REAL ``DnsServer`` on a UDP socket and drives it with real DNS packets,
so the full ``_handle_packet -> _resolve_internal -> _udp_query`` path runs
over the wire. It is the no-cluster stand-in for the Kubernetes leg of
contract case T-NET-3: an internal ``*.cluster.local`` name is forwarded to
a stub kube-dns and resolves, while an external name is NXDOMAINed without
either upstream being touched.

The stubs record which upstream actually received each query, so the test
proves the routing — not just the rcode. A green here plus the live-gateway
``nats -> NOERROR`` probe (Docker embedded DNS leg) covers both runtimes'
forwarding without standing up a cluster.
"""

from __future__ import annotations

import asyncio

import dnslib
import pytest

from rolemesh.egress.dns_policy import GlobalDnsPolicy
from rolemesh.egress.dns_resolver import DnsServer, UpstreamResolver

pytestmark = pytest.mark.asyncio


class _StubResolver(asyncio.DatagramProtocol):
    """A canned upstream that answers every A query with one fixed record
    and remembers the qnames it was asked, so a test can assert routing."""

    def __init__(self, answer_ip: str) -> None:
        self._answer_ip = answer_ip
        self.received: list[str] = []
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        query = dnslib.DNSRecord.parse(data)
        self.received.append(str(query.q.qname).rstrip("."))
        reply = query.reply()
        reply.add_answer(
            *dnslib.RR.fromZone(f"{query.q.qname} 30 A {self._answer_ip}")
        )
        assert self._transport is not None
        self._transport.sendto(reply.pack(), addr)


async def _bind(proto: _StubResolver) -> tuple[asyncio.DatagramTransport, int]:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: proto, local_addr=("127.0.0.1", 0)
    )
    port = transport.get_extra_info("socket").getsockname()[1]
    return transport, port


async def _ask(resolver_port: int, qname: str) -> dnslib.DNSRecord:
    """Send one real A query to 127.0.0.1:resolver_port and parse the reply."""
    loop = asyncio.get_running_loop()
    recv: asyncio.Future[bytes] = loop.create_future()

    class _Client(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            if not recv.done():
                recv.set_result(data)

    transport, _ = await loop.create_datagram_endpoint(
        _Client, remote_addr=("127.0.0.1", resolver_port)
    )
    try:
        transport.sendto(dnslib.DNSRecord.question(qname, "A").pack())
        data = await asyncio.wait_for(recv, timeout=2.0)
    finally:
        transport.close()
    return dnslib.DNSRecord.parse(data)


def _is_cluster_local(name: str) -> bool:
    n = name.rstrip(".").lower()
    return n == "cluster.local" or n.endswith(".cluster.local")


async def test_internal_cluster_local_forwarded_to_kube_dns_over_udp() -> None:
    kube_dns = _StubResolver("10.96.0.99")
    external = _StubResolver("203.0.113.1")  # must never be hit by internal
    kube_t, kube_port = await _bind(kube_dns)
    ext_t, ext_port = await _bind(external)

    server = DnsServer(
        policy=GlobalDnsPolicy(),  # empty allowlist + enforce (steady state)
        upstreams=[UpstreamResolver(host="127.0.0.1", port=ext_port)],
        internal_matcher=_is_cluster_local,
        internal_upstreams=[UpstreamResolver(host="127.0.0.1", port=kube_port)],
    )
    await server.serve("127.0.0.1", 0)
    server_port = server._transport.get_extra_info("socket").getsockname()[1]  # type: ignore[union-attr]
    try:
        # Internal name: resolves via kube-dns, NEVER via the external leg.
        internal = await _ask(server_port, "nats.rolemesh.svc.cluster.local")
        assert dnslib.RCODE.get(internal.header.rcode) == "NOERROR"
        assert str(internal.rr[0].rdata) == "10.96.0.99"
        assert kube_dns.received == ["nats.rolemesh.svc.cluster.local"]
        assert external.received == []

        # External name: blocked at the allowlist, reaching NEITHER upstream.
        ext = await _ask(server_port, "secret-payload.attacker.com")
        assert dnslib.RCODE.get(ext.header.rcode) == "NXDOMAIN"
        assert external.received == []
        assert kube_dns.received == ["nats.rolemesh.svc.cluster.local"]
    finally:
        server.close()
        kube_t.close()
        ext_t.close()
