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

from typing import TYPE_CHECKING

import dnslib
import pytest

from rolemesh.egress.dns_policy import GlobalDnsPolicy
from rolemesh.egress.dns_resolver import DnsServer, UpstreamResolver

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio


_SOURCE = ("172.30.0.9", 53535)


def _query(qname: str, qtype: str) -> bytes:
    return dnslib.DNSRecord.question(qname, qtype).pack()


def _rcode(packet: bytes) -> str:
    parsed = dnslib.DNSRecord.parse(packet)
    return dnslib.RCODE.get(parsed.header.rcode)


class _Harness:
    """DnsServer with both upstream legs replaced by canned answers.

    ``upstream_calls`` / ``internal_calls`` record which leg fired, so a
    test can assert not just the rcode but that an exfil name never even
    reached a forwarder, and an internal name reached the *internal* one.
    """

    def __init__(
        self,
        policy: GlobalDnsPolicy,
        *,
        upstream_answers: bool = True,
        internal_matcher: Callable[[str], bool] | None = None,
    ) -> None:
        self.server = DnsServer(
            policy=policy,
            upstreams=[UpstreamResolver(host="192.0.2.1")],  # TEST-NET, never dialed
            internal_matcher=internal_matcher,
            internal_upstreams=(
                [UpstreamResolver(host="127.0.0.11")] if internal_matcher else None
            ),
        )
        self.upstream_calls: list[bytes] = []
        self.internal_calls: list[bytes] = []
        self.responses: list[bytes] = []

        def _answer(data: bytes) -> bytes:
            reply = dnslib.DNSRecord.parse(data).reply()
            reply.add_answer(*dnslib.RR.fromZone("resolved.test. 60 A 198.51.100.7"))
            return reply.pack()

        async def _fake_upstream(data: bytes) -> bytes | None:
            self.upstream_calls.append(data)
            return _answer(data) if upstream_answers else None

        async def _fake_internal(data: bytes) -> bytes | None:
            self.internal_calls.append(data)
            return _answer(data)

        self.server._resolve_upstream = _fake_upstream  # type: ignore[method-assign]
        self.server._resolve_internal = _fake_internal  # type: ignore[method-assign]

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


# --------------------------------------------------------------------------
# Internal-name exemption: internal forwarded, external still tripwired.
# The matcher here calls anything ending in ".svc.internal" internal.
# --------------------------------------------------------------------------

_INTERNAL = lambda name: name.endswith(".svc.internal")  # noqa: E731


async def test_internal_name_forwarded_despite_empty_allowlist() -> None:
    # The whole point: with the steady-state EMPTY allowlist + enforce, an
    # internal name must still resolve — via the INTERNAL leg, never the
    # external one, never the allowlist.
    h = _Harness(GlobalDnsPolicy(), internal_matcher=_INTERNAL)
    await h.ask("nats.svc.internal", "A")
    assert [_rcode(r) for r in h.responses] == ["NOERROR"]
    assert len(h.internal_calls) == 1
    assert h.upstream_calls == []


async def test_external_name_still_nxdomain_when_exemption_active() -> None:
    # Turning the exemption on must not punch a hole for external names:
    # a non-matching, non-allowlisted name is still blocked, and reaches
    # NEITHER forwarder (the attacker's NS records no hit).
    h = _Harness(GlobalDnsPolicy(), internal_matcher=_INTERNAL)
    await h.ask("secret-payload.attacker.com", "A")
    assert [_rcode(r) for r in h.responses] == ["NXDOMAIN"]
    assert h.internal_calls == []
    assert h.upstream_calls == []


async def test_internal_txt_refused_by_qtype_gate_before_exemption() -> None:
    # The qtype gate runs before the exemption, so an internal name cannot
    # be used to smuggle a TXT/SRV tunnel through the internal resolver.
    h = _Harness(GlobalDnsPolicy(), internal_matcher=_INTERNAL)
    await h.ask("nats.svc.internal", "TXT")
    assert [_rcode(r) for r in h.responses] == ["REFUSED"]
    assert h.internal_calls == []
    assert h.upstream_calls == []


async def test_allowlisted_external_uses_external_leg_not_internal() -> None:
    # An allowlisted external name still goes out the EXTERNAL upstream even
    # when the exemption is wired — the two legs must not cross.
    h = _Harness(
        GlobalDnsPolicy(patterns=("*.corp.test",)), internal_matcher=_INTERNAL
    )
    await h.ask("metrics.corp.test", "A")
    assert [_rcode(r) for r in h.responses] == ["NOERROR"]
    assert len(h.upstream_calls) == 1
    assert h.internal_calls == []


async def test_matcher_and_upstream_must_be_wired_together() -> None:
    # An exemption half-configured is a deploy bug that would silently
    # NXDOMAIN every internal name — fail loud at construction instead.
    policy = GlobalDnsPolicy()
    ext = [UpstreamResolver(host="192.0.2.1")]
    with pytest.raises(ValueError):
        DnsServer(policy=policy, upstreams=ext, internal_matcher=_INTERNAL)
    with pytest.raises(ValueError):
        DnsServer(policy=policy, upstreams=ext, internal_upstreams=ext)
