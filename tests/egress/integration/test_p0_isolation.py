"""P0#1 — EC-1 four-piece network isolation (§5.2 of the EC design).

These are the gate tests for EC-1. If any fails, every later EC-2/EC-3
test is built on sand:

  1. Direct TCP from an agent bridge → the public internet is blocked.
     This is the physical ``Internal=true`` guarantee.
  2. Metadata IMDS (169.254.169.254) is blackholed in ``/etc/hosts``.
  3. The egress gateway is reachable by its service name on the
     internal bridge.
  4. The orchestrator injects HTTP_PROXY / HTTPS_PROXY env so
     downstream HTTP clients automatically route via the gateway.
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


async def test_direct_internet_tcp_blocked(
    topology: Topology, per_test_cleanup: None
) -> None:
    """An agent on the Internal=true bridge must NOT be able to TCP-connect
    to a public IP.

    Using 1.1.1.1:443 rather than 8.8.8.8 because Cloudflare DNS is more
    geographically stable in CI (Google can route to a regional POP with
    oddball behaviour).
    """
    probe = await topology.spawn_probe()
    rc, out = await probe.exec_sh(
        """
python3 - <<'PY'
import socket
s = socket.socket()
s.settimeout(3)
try:
    s.connect(('1.1.1.1', 443))
    print("FAIL: connected")
except (TimeoutError, OSError) as exc:
    print(f"OK: blocked ({type(exc).__name__})")
PY
"""
    )
    assert rc == 0, out
    assert "OK: blocked" in out, (
        f"Expected direct internet blocked on Internal=true bridge, got: {out}"
    )


async def test_metadata_ip_blackholed(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Cloud metadata IP is rewritten to 127.0.0.1 via ExtraHosts.

    Even if a future regression re-opens the bridge, the metadata
    endpoint must remain unreachable because an agent that can reach
    IMDS exfiltrates IAM creds with a single ``curl``.
    """
    probe = await topology.spawn_probe()
    rc, out = await probe.exec_sh(
        """
python3 - <<'PY'
import socket
s = socket.socket()
s.settimeout(2)
try:
    s.connect(('169.254.169.254', 80))
    print("FAIL: metadata reachable")
except (TimeoutError, OSError) as exc:
    print(f"OK: blocked ({type(exc).__name__})")
PY
"""
    )
    assert rc == 0, out
    assert "OK: blocked" in out, f"Metadata IP must be unreachable, got: {out}"


async def test_gateway_healthz_reachable_by_service_name(
    topology: Topology, per_test_cleanup: None
) -> None:
    """The gateway answers /healthz on port 3001 via Docker embedded DNS.

    Uses the 'egress-gateway' alias we attached to the container's
    agent-net endpoint in conftest — mirrors the production alias.
    """
    probe = await topology.spawn_probe()
    rc, out = await probe.exec_sh(
        """
python3 - <<'PY'
import urllib.request
r = urllib.request.urlopen('http://egress-gateway:3001/healthz', timeout=5)
print(f"status={r.status}")
print(f"body={r.read().decode()}")
PY
"""
    )
    assert rc == 0, out
    assert "status=200" in out, out
    assert "body=ok" in out, out


async def test_https_proxy_env_set(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Baseline: the probe fixture sets HTTP_PROXY / HTTPS_PROXY so
    any stdlib HTTP client automatically uses the gateway. If this
    fails the helper drifted from what the orchestrator does in
    production (see container/runner.py build_container_spec)."""
    probe = await topology.spawn_probe()
    rc, out = await probe.exec_sh("env | sort | grep -Ei '^(http|https|no)_proxy'")
    assert rc == 0, out
    assert "HTTP_PROXY=http://egress-gateway:3128" in out, out
    assert "HTTPS_PROXY=http://egress-gateway:3128" in out, out
    assert "egress-gateway" in out, out  # NO_PROXY carve-out
