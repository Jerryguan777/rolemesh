"""#7 — Gateway down blocks all egress (EC-1 physical-layer guarantee).

Confirms that killing the gateway container doesn't silently leave a
fallback path for agents. With ``Internal=true`` on the agent bridge,
the ONLY outbound route is through the gateway — so every outbound
path (CONNECT, plain HTTP via proxy, reverse proxy, DNS) must fail.

This test runs LAST in the module because it stops the session-scoped
gateway. The ``force_gateway_restart`` fixture restarts the gateway
after the test so any later test module still has a working topology.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .helpers import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(loop_scope="session")
async def restart_gateway_after(topology: Topology) -> AsyncIterator[None]:
    """Start the gateway back up after this test so the topology is
    reusable by later tests / re-runs."""
    yield
    # The stop_gateway call below left the container stopped. Re-start
    # it rather than recreate (preserves IP / network attachment).
    c = topology.docker.containers.container(topology.gateway.name)
    with contextlib.suppress(Exception):
        await c.start()
    # Wait for /healthz to come back so any subsequent test in the
    # same session sees a functional gateway.
    from .helpers import wait_for_http_ok

    with contextlib.suppress(Exception):
        await wait_for_http_ok(
            topology.docker,
            network=topology.agent_network,
            url="http://egress-gateway:3001/healthz",
            attempts=30,
        )


async def test_gateway_down_blocks_all_egress_paths(
    topology: Topology,
    per_test_cleanup: None,
    restart_gateway_after: None,
) -> None:
    """Every outbound path the agent can try fails when the gateway is
    down.

    Asserts on three paths: HTTPS CONNECT (forward proxy), reverse
    proxy, and DNS resolution. Any of them unexpectedly succeeding
    would invalidate the EC-1 physical-isolation claim.
    """
    probe = await topology.spawn_probe()
    await topology.publish_lifecycle_started(
        probe,
        identity={
            "tenant_id": "tenant-a",
            "coworker_id": "coworker-x",
            "user_id": "u",
            "conversation_id": "c",
            "job_id": "job-gw-down",
        },
    )
    # Sanity check: gateway reachable now.
    rc, out = await probe.exec_sh(
        "python3 -c \"import urllib.request; "
        "print(urllib.request.urlopen('http://egress-gateway:3001/healthz', timeout=3).status)\""
    )
    assert rc == 0 and "200" in out, (
        f"Pre-flight: gateway must be reachable before we kill it; got {out}"
    )

    # Stop gateway.
    await topology.stop_gateway()
    # Give Docker a moment to tear down the bridge endpoint.
    await asyncio.sleep(1.0)

    # --- Path 1: forward-proxy CONNECT ---
    rc, out = await probe.exec_sh(
        """
python3 - <<'PY'
import socket
s = socket.socket()
s.settimeout(3)
try:
    s.connect(('egress-gateway', 3128))
    s.sendall(b'CONNECT evil.test:443 HTTP/1.1\\r\\nHost: evil.test\\r\\n\\r\\n')
    chunk = s.recv(1024)
    print(f'UNEXPECTED={chunk[:80]!r}')
except (TimeoutError, ConnectionRefusedError, OSError) as exc:
    print(f'BLOCKED={type(exc).__name__}')
PY
"""
    )
    assert rc == 0, out
    assert "BLOCKED=" in out, f"CONNECT must fail with gateway down: {out}"

    # --- Path 2: reverse proxy ---
    rc, out = await probe.exec_sh(
        """
python3 - <<'PY'
import urllib.request, urllib.error
try:
    urllib.request.urlopen(
        'http://egress-gateway:3001/proxy/anthropic/v1/messages',
        data=b'{}',
        timeout=3,
    )
    print('UNEXPECTED=reached-upstream')
except (urllib.error.URLError, TimeoutError, OSError) as exc:
    print(f'BLOCKED={type(exc).__name__}')
PY
"""
    )
    assert rc == 0, out
    assert "BLOCKED=" in out, f"Reverse proxy must fail with gateway down: {out}"

    # --- Path 3: DNS ---
    # Agents on the bridge have the gateway pinned as their DNS.
    # With gateway down, DNS queries should time out (we pin a tight
    # budget so the test doesn't stall).
    rc, out = await probe.exec_sh(
        f"""
python3 - <<'PY'
import socket, struct

def encode_name(name):
    parts = name.split('.')
    out = b''
    for p in parts:
        out += bytes([len(p)]) + p.encode()
    out += b'\\x00'
    return out

packet = struct.pack('>HHHHHH', 0x1234, 0x0100, 1, 0, 0, 0)
packet += encode_name('api.anthropic.com') + struct.pack('>HH', 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
try:
    s.sendto(packet, ('{topology.gateway_ip_on_agent_net}', 53))
    s.recvfrom(4096)
    print('UNEXPECTED=got-response')
except (TimeoutError, ConnectionRefusedError, OSError) as exc:
    print(f'BLOCKED={{type(exc).__name__}}')
PY
"""
    )
    assert rc == 0, out
    assert "BLOCKED=" in out, f"DNS must fail with gateway down: {out}"
