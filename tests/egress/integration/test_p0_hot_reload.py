"""P0#4 — Policy cache hot-reload via ``safety.rule.changed`` (§7.2 of the design).

Exercises the live update path: an admin disables a rule, the gateway
picks up the delta via NATS without restarting, and subsequent
requests get the new verdict.

This is the capability that makes safety_rules operator-usable in
production — without it, every rule change would require a gateway
redeploy. Pinning the behaviour here catches a regression where the
subscriber silently drops events or where ``apply_event`` races with
the in-flight request.
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


def _connect_script(host: str, port: int) -> str:
    """Minimal CONNECT probe — success vs 403 is all we care about here."""
    return f"""
import socket
s = socket.socket()
s.settimeout(5)
s.connect(('egress-gateway', 3128))
s.sendall(b'CONNECT {host}:{port} HTTP/1.1\\r\\nHost: {host}:{port}\\r\\n\\r\\n')
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
line = head.splitlines()[0] if head else ''
print(f'STATUS={{line!r}}')
try:
    s.close()
except OSError:
    pass
"""


async def test_rule_disable_propagates_to_live_gateway(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Initially the rule allows the fake upstream → CONNECT succeeds.
    After a ``safety.rule.changed`` event disables the rule, CONNECT
    to the same host must be blocked — same agent container, no
    gateway restart between.

    The policy cache is session-scoped (lives inside the gateway
    container), so rules created by earlier tests in the same session
    are still present. We purge every known earlier test's rule
    before seeding our own so the verdict isn't contaminated by a
    leftover allowlist entry matching the same host.
    """

    # Purge rules left behind by earlier tests in this session. Delete
    # events for unknown rule_ids are no-ops in the cache, so listing
    # them here rather than tracking state across fixtures stays
    # simple.
    for leftover_id in ("rule-p0-2", "rule-p0-3"):
        await topology.publish_rule_changed(
            "deleted", {"id": leftover_id, "rule_id": leftover_id, "stage": "egress_request"}
        )

    rule = {
        "id": "rule-p0-4",
        "rule_id": "rule-p0-4",
        "tenant_id": TENANT_ID,
        "coworker_id": None,
        "stage": "egress_request",
        "check_id": "egress.domain_rule",
        "config": {"domain_pattern": topology.fake_upstream_name},
        "priority": 100,
        "enabled": True,
    }
    await topology.seed_rules_responder([rule])
    await topology.publish_rule_changed("created", rule)

    probe = await topology.spawn_probe()
    await topology.publish_lifecycle_started(
        probe,
        identity={
            "tenant_id": TENANT_ID,
            "coworker_id": COWORKER_ID,
            "user_id": "u",
            "conversation_id": "c",
            "job_id": "job-p0-4",
        },
    )

    # --- Before: allow ---
    rc1, out1 = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script(topology.fake_upstream_name, 443)}\nPY"
    )
    assert rc1 == 0, out1
    assert "STATUS='HTTP/1.1 200" in out1, (
        f"Initial CONNECT should succeed under the allow rule; got: {out1}"
    )

    # --- Disable the rule via hot reload ---
    disabled = dict(rule)
    disabled["enabled"] = False
    await topology.publish_rule_changed("updated", disabled)

    # --- After: block ---
    rc2, out2 = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script(topology.fake_upstream_name, 443)}\nPY"
    )
    assert rc2 == 0, out2
    # The reason phrase comes from safety_call.py — catches a
    # regression where the cache accepted the delta but the request
    # path still read from the pre-delta snapshot.
    assert "STATUS='HTTP/1.1 403" in out2, (
        f"CONNECT after rule disable should block; got: {out2}"
    )
