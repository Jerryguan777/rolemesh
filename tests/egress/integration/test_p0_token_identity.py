"""P0 — token identity drives the forward proxy (token-identity refactor).

These prove the gateway recovers identity from the SIGNED TOKEN the
agent presents in ``Proxy-Authorization``. Both cases send a *valid*
token for a specific tenant, and the rule is scoped to that tenant, so
the allow/block verdict can only come from the token's claims.

  1. Token for ``tenant-tok``, which has an allow rule for the upstream
     → CONNECT tunnels (200). Only the token's tenant has the rule, so a
     200 can only mean the gateway read the tenant FROM THE TOKEN.
  2. Token for ``tenant-nope``, which has NO rule → CONNECT blocked
     (403), even though ``tenant-tok`` allows that same host. Proves the
     decision is scoped to the token's tenant, not "any allow rule".

The probe is spawned WITHOUT a lifecycle event; identity can only come
from the token. We send the CONNECT over a raw socket (the gateway is
not a TLS terminator) and set ``Proxy-Authorization: Basic`` by hand —
the production HTTP_PROXY userinfo turns into exactly this header, but
the raw-socket probe doesn't read env, so we build it explicitly.

Run on a Docker host: ``pytest tests/egress/integration -m integration``.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from .helpers import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


def _connect_with_token_script(host: str, port: int, token: str) -> str:
    """Raw CONNECT carrying ``Proxy-Authorization: Basic job:<token>``.

    Mirrors what a real client does when it sees
    ``HTTP_PROXY=http://job:<token>@gateway`` — but the raw socket can't
    read env, so we set the header ourselves.
    """
    cred = base64.b64encode(f"job:{token}".encode()).decode()
    return f"""
import socket

s = socket.socket()
s.settimeout(5)
s.connect(('egress-gateway', 3128))
req = (
    b'CONNECT {host}:{port} HTTP/1.1\\r\\n'
    b'Host: {host}:{port}\\r\\n'
    b'Proxy-Authorization: Basic {cred}\\r\\n'
    b'\\r\\n'
)
s.sendall(req)
buf = b''
while b'\\r\\n\\r\\n' not in buf:
    try:
        chunk = s.recv(4096)
    except (ConnectionResetError, OSError):
        break
    if not chunk:
        break
    buf += chunk
    if len(buf) > 8192:
        break
head = buf.split(b'\\r\\n\\r\\n', 1)[0].decode('latin-1', errors='replace')
status_line = head.splitlines()[0] if head else ''
print(f'PROXY_STATUS={{status_line!r}}')
if '200' in status_line:
    try:
        s.sendall(b'GET / HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n')
    except (ConnectionResetError, OSError):
        pass
    body = b''
    while True:
        try:
            chunk = s.recv(4096)
        except (ConnectionResetError, OSError):
            break
        if not chunk:
            break
        body += chunk
        if len(body) > 32768:
            break
    if b'"path":' in body:
        print('UPSTREAM_ECHOED_OK')
try:
    s.close()
except OSError:
    pass
"""


async def _seed_rule_for(topology: Topology, tenant_id: str, allow_host: str) -> None:
    rule = {
        "id": f"rule-tok-{tenant_id}",
        "rule_id": f"rule-tok-{tenant_id}",
        "tenant_id": tenant_id,
        "coworker_id": None,
        "stage": "egress_request",
        "check_id": "egress.domain_rule",
        "config": {"domain_patterns": [allow_host]},
        "priority": 100,
        "enabled": True,
    }
    await topology.seed_rules_responder([rule])
    await topology.publish_rule_changed("created", rule)


async def test_token_tenant_with_rule_is_allowed(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Valid token for the tenant that owns the allow rule → 200. No
    lifecycle event, so identity can only have come from the token."""
    await _seed_rule_for(topology, "tenant-tok", topology.fake_upstream_name)

    token = topology.mint_token(
        {
            "tenant_id": "tenant-tok",
            "coworker_id": "cow-tok",
            "user_id": "user-tok",
            "conversation_id": "conv-tok",
            "job_id": "job-tok-allow",
        }
    )
    probe = await topology.spawn_probe(egress_token=token)

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_with_token_script(topology.fake_upstream_name, 443, token)}\nPY"
    )
    assert rc == 0, out
    assert "PROXY_STATUS='HTTP/1.1 200" in out, out
    assert "UPSTREAM_ECHOED_OK" in out, out


async def test_token_tenant_without_rule_is_blocked(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Valid token for a DIFFERENT tenant with no rule → 403, even though
    tenant-tok allows this host. The decision is scoped to the token's
    tenant, and the token (being valid) wins over any source-IP state."""
    await _seed_rule_for(topology, "tenant-tok", topology.fake_upstream_name)

    token = topology.mint_token(
        {
            "tenant_id": "tenant-nope",
            "coworker_id": "cow-nope",
            "user_id": "user-nope",
            "conversation_id": "conv-nope",
            "job_id": "job-nope-block",
        }
    )
    probe = await topology.spawn_probe(egress_token=token)

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_with_token_script(topology.fake_upstream_name, 443, token)}\nPY"
    )
    assert rc == 0, out
    assert "PROXY_STATUS='HTTP/1.1 403" in out, out
