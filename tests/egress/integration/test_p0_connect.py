"""P0#2 — Forward-proxy CONNECT allow/block + audit (§6.3 of the EC design).

End-to-end exercise: an agent container sees its CONNECT request gated
by a domain allowlist stored in the gateway's policy cache (seeded via
the NATS snapshot RPC). Allow path tunnels bytes to a fake upstream.
Block path returns 403 with ``X-Egress-Reason``. Either way, a
``safety_decisions`` audit row lands on ``agent.<job_id>.safety_events``.

We use raw-socket CONNECT (no TLS) because:
  * The gateway is NOT a TLS terminator — it pipes bytes verbatim.
  * The fake upstream speaks plain HTTP on port 443.
  * Running a real TLS server adds cert-distribution noise that has no
    bearing on what CONNECT-path tests are supposed to prove.
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


TENANT_ID = "tenant-a"
COWORKER_ID = "coworker-x"


def _connect_script(host: str, port: int, path: str = "/") -> str:
    """Python snippet for raw CONNECT + HTTP GET through the tunnel.

    Emits structured lines the test parses with regex — stdout is the
    only way back out of ``docker exec``.
    """
    return f"""
import socket

s = socket.socket()
s.settimeout(5)
s.connect(('egress-gateway', 3128))
req = b'CONNECT {host}:{port} HTTP/1.1\\r\\nHost: {host}:{port}\\r\\n\\r\\n'
s.sendall(req)
# Read status line + headers from proxy response.
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
# If proxy said 200, send a plain GET through the tunnel and read the echo.
if '200' in status_line:
    try:
        s.sendall(b'GET {path} HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n')
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
    print(f'UPSTREAM_BYTES={{len(body)}}')
    if b'"path":' in body:
        print('UPSTREAM_ECHOED_OK')
else:
    for line in head.splitlines():
        if line.lower().startswith('x-egress-reason'):
            print(f'{{line}}')
try:
    s.close()
except OSError:
    pass
"""


async def _bootstrap_rules(topology: Topology, *, allow_host: str) -> list[dict[str, object]]:
    """Seed the gateway's policy cache with a rule allowing *allow_host*.

    Done via a NATS responder that replies on
    ``egress.rules.snapshot.request``; the gateway fetches at boot but
    we also issue a ``safety.rule.changed`` event so a running gateway
    picks up the rule without restart.
    """
    rule = {
        "id": "rule-p0-2",
        "rule_id": "rule-p0-2",
        "tenant_id": TENANT_ID,
        "coworker_id": None,
        "stage": "egress_request",
        "check_id": "egress.domain_rule",
        "config": {"domain_pattern": allow_host},
        "priority": 100,
        "enabled": True,
    }
    # Replace the session-level empty responder for future gateway
    # restarts + publish a live delta for the running gateway.
    await topology.seed_rules_responder([rule])
    await topology.publish_rule_changed("created", rule)
    return [rule]


async def test_connect_allow_path_tunnels_bytes(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Rule allows the fake upstream → gateway returns 200 + bytes flow end-to-end."""
    await _bootstrap_rules(topology, allow_host=topology.fake_upstream_name)

    probe = await topology.spawn_probe()
    await topology.publish_lifecycle_started(
        probe,
        identity={
            "tenant_id": TENANT_ID,
            "coworker_id": COWORKER_ID,
            "user_id": "user-1",
            "conversation_id": "conv-1",
            "job_id": "job-p0-2-allow",
        },
    )

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script(topology.fake_upstream_name, 443)}\nPY"
    )
    # On any assertion failure, dump gateway + fake upstream logs so we
    # see the whole chain in one shot.
    if "UPSTREAM_ECHOED_OK" not in out:
        import contextlib as _c
        for name, handle in [
            ("GATEWAY", topology.gateway),
            ("FAKE_UPSTREAM", topology.fake_upstream),
        ]:
            with _c.suppress(Exception):
                c = topology.docker.containers.container(handle.name)
                logs = await c.log(stdout=True, stderr=True)
                out = out + f"\n\n=== {name} LOGS ===\n" + "".join(logs)[-2000:]

    assert rc == 0, out
    assert "PROXY_STATUS='HTTP/1.1 200" in out, out
    assert "UPSTREAM_ECHOED_OK" in out, out


async def test_connect_block_path_returns_403_with_reason(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Any other host → 403 + ``X-Egress-Reason`` header + NO tunnel."""
    # Rule from the previous test may still be in cache; we just need
    # to pick a host that doesn't match it.
    await _bootstrap_rules(topology, allow_host=topology.fake_upstream_name)

    probe = await topology.spawn_probe()
    await topology.publish_lifecycle_started(
        probe,
        identity={
            "tenant_id": TENANT_ID,
            "coworker_id": COWORKER_ID,
            "user_id": "user-1",
            "conversation_id": "conv-1",
            "job_id": "job-p0-2-block",
        },
    )

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script('evil.test', 443)}\nPY"
    )
    assert rc == 0, out
    assert "PROXY_STATUS='HTTP/1.1 403" in out, out
    # The reason header carries the denial message — catches a
    # regression where the gateway returns 403 but strips the header.
    assert "X-Egress-Reason" in out, out
    # The tunnel must never have been opened.
    assert "UPSTREAM_ECHOED_OK" not in out, out


async def test_connect_audit_events_published(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Both allow and block write a safety_decisions event to
    ``agent.<job_id>.safety_events``.

    We capture from NATS directly (faster than querying postgres and
    the subscriber's trust-check is already covered by unit tests)."""
    import asyncio

    await _bootstrap_rules(topology, allow_host=topology.fake_upstream_name)

    probe = await topology.spawn_probe()
    await topology.publish_lifecycle_started(
        probe,
        identity={
            "tenant_id": TENANT_ID,
            "coworker_id": COWORKER_ID,
            "user_id": "user-1",
            "conversation_id": "conv-1",
            "job_id": "job-p0-2-audit",
        },
    )

    # Start the capture BEFORE firing requests so we don't race the
    # publish.
    capture_task = asyncio.create_task(
        topology.capture_safety_events(duration_s=4.0)
    )
    await asyncio.sleep(0.3)

    # One allow + one block under the same identity.
    await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script(topology.fake_upstream_name, 443)}\nPY"
    )
    await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script('evil.test', 443)}\nPY"
    )

    events = await capture_task
    # Filter to this test's job so parallel runs don't cross-pollinate.
    mine = [e for e in events if e.get("job_id") == "job-p0-2-audit"]
    actions = sorted(e.get("verdict_action") for e in mine)
    assert actions == ["allow", "block"], (
        f"Expected one allow + one block audit row, got {mine}"
    )
    # Allow event carries the rule id it matched.
    allow_event = next(e for e in mine if e["verdict_action"] == "allow")
    assert "rule-p0-2" in allow_event.get("triggered_rule_ids", []), allow_event
