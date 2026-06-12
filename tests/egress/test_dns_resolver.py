"""Unit tests for DnsServer's packet handler against the platform policy.

These drive ``_handle_packet`` directly with real dnslib-built packets
(dnslib is mirrored into the dev extra for exactly this) and stub the
upstream recursion, so the full qtype-gate → policy → respond pipeline
runs without sockets.

Invariants pinned:

  * qtype outside {A, AAAA, CNAME} → REFUSED, upstream never contacted
    — even for an allowlisted name (exfil via TXT under a whitelisted
    apex must stay closed).
  * enforce + non-allowlisted name → NXDOMAIN, upstream never contacted
    (the attacker's authoritative NS records no hit).
  * enforce + allowlisted name → upstream response forwarded verbatim.
  * observe + non-allowlisted name → resolves (logged, not blocked).
  * upstream outage on an allowed query → SERVFAIL, not NXDOMAIN.
"""

from __future__ import annotations

import dnslib
import pytest

from rolemesh.egress.dns_policy import GlobalDnsPolicy
from rolemesh.egress.dns_resolver import DnsServer, UpstreamResolver

pytestmark = pytest.mark.asyncio


_SOURCE = ("172.30.0.9", 53535)


def _query(qname: str, qtype: str) -> bytes:
    return dnslib.DNSRecord.question(qname, qtype).pack()


def _rcode(packet: bytes) -> str:
    parsed = dnslib.DNSRecord.parse(packet)
    return dnslib.RCODE.get(parsed.header.rcode)


class _Harness:
    """DnsServer with the upstream leg replaced by a canned answer."""

    def __init__(self, policy: GlobalDnsPolicy, *, upstream_answers: bool = True) -> None:
        self.server = DnsServer(
            policy=policy,
            upstreams=[UpstreamResolver(host="192.0.2.1")],  # TEST-NET, never dialed
        )
        self.upstream_calls: list[bytes] = []
        self.responses: list[bytes] = []

        async def _fake_upstream(data: bytes) -> bytes | None:
            self.upstream_calls.append(data)
            if not upstream_answers:
                return None
            reply = dnslib.DNSRecord.parse(data).reply()
            reply.add_answer(
                *dnslib.RR.fromZone("resolved.test. 60 A 198.51.100.7")
            )
            return reply.pack()

        self.server._resolve_upstream = _fake_upstream  # type: ignore[method-assign]

    async def ask(self, qname: str, qtype: str) -> None:
        await self.server._handle_packet(
            _query(qname, qtype), _SOURCE, self.responses.append
        )


async def test_txt_refused_even_for_allowlisted_name() -> None:
    h = _Harness(GlobalDnsPolicy(patterns=("allowed.test",)))
    await h.ask("allowed.test", "TXT")
    assert [_rcode(r) for r in h.responses] == ["REFUSED"]
    assert h.upstream_calls == []


async def test_enforce_blocks_non_allowlisted_with_nxdomain() -> None:
    h = _Harness(GlobalDnsPolicy())
    await h.ask("secret-payload.evil.test", "A")
    assert [_rcode(r) for r in h.responses] == ["NXDOMAIN"]
    assert h.upstream_calls == []


async def test_enforce_allows_allowlisted_via_upstream() -> None:
    h = _Harness(GlobalDnsPolicy(patterns=("*.corp.test",)))
    await h.ask("metrics.corp.test", "A")
    assert len(h.upstream_calls) == 1
    assert [_rcode(r) for r in h.responses] == ["NOERROR"]


async def test_observe_resolves_non_allowlisted() -> None:
    h = _Harness(GlobalDnsPolicy(mode="observe"))
    await h.ask("not-on-any-list.test", "A")
    assert len(h.upstream_calls) == 1
    assert [_rcode(r) for r in h.responses] == ["NOERROR"]


async def test_upstream_outage_is_servfail_not_nxdomain() -> None:
    """Operators must be able to tell 'blocked' from 'resolver down'."""
    h = _Harness(GlobalDnsPolicy(patterns=("allowed.test",)), upstream_answers=False)
    await h.ask("allowed.test", "A")
    assert [_rcode(r) for r in h.responses] == ["SERVFAIL"]


async def test_multi_question_packet_refused() -> None:
    record = dnslib.DNSRecord.question("a.test", "A")
    record.add_question(dnslib.DNSQuestion("b.test"))
    h = _Harness(GlobalDnsPolicy(patterns=("a.test", "b.test")))
    await h.server._handle_packet(record.pack(), _SOURCE, h.responses.append)
    assert [_rcode(r) for r in h.responses] == ["REFUSED"]
    assert h.upstream_calls == []
