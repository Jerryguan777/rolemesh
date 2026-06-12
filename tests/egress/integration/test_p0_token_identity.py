"""P0 — token identity drives the forward proxy (token-identity refactor).

These prove the gateway recovers an agent's identity from the SIGNED
TOKEN in its proxy env, with NO source-IP lifecycle registration:

  1. A probe carrying a token for ``tenant-tok`` + an allow rule scoped
     to that tenant → CONNECT to the allowed host tunnels (200). The
     gateway must have read the tenant FROM THE TOKEN to pick the rule,
     because no lifecycle event ever told it this IP's identity.
  2. A probe carrying NO token and NO lifecycle → CONNECT blocked (403):
     no identity from either channel, default-deny holds.

The reverse-proxy + DNS planes are covered by their own suites; the
forward proxy is the cleanest place to prove token identity end-to-end
because a domain-rule allow/block is decided purely from identity +
host, with no credential responder in the loop.

Run on a Docker host: ``pytest tests/egress/integration -m integration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .test_p0_connect import _bootstrap_rules, _connect_script

if TYPE_CHECKING:
    from .helpers import Topology

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


TOKEN_TENANT = "tenant-tok"


async def test_token_identity_selects_tenant_rules(
    topology: Topology, per_test_cleanup: None
) -> None:
    """Allow rule is scoped to the TOKEN's tenant; the probe registers no
    IP identity. A 200 tunnel proves the gateway read the tenant from the
    token."""
    # Rule allows the fake upstream, scoped to the token's tenant.
    rule = {
        "id": "rule-tok-1",
        "rule_id": "rule-tok-1",
        "tenant_id": TOKEN_TENANT,
        "coworker_id": None,
        "stage": "egress_request",
        "check_id": "egress.domain_rule",
        "config": {"domain_patterns": [topology.fake_upstream_name]},
        "priority": 100,
        "enabled": True,
    }
    await topology.seed_rules_responder([rule])
    await topology.publish_rule_changed("created", rule)

    token = topology.mint_token(
        {
            "tenant_id": TOKEN_TENANT,
            "coworker_id": "cow-tok",
            "user_id": "user-tok",
            "conversation_id": "conv-tok",
            "job_id": "job-tok-allow",
        }
    )
    # NOTE: deliberately NO publish_lifecycle_started — identity must
    # come from the token alone.
    probe = await topology.spawn_probe(egress_token=token)

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script(topology.fake_upstream_name, 443)}\nPY"
    )
    assert rc == 0, out
    assert "PROXY_STATUS='HTTP/1.1 200" in out, out
    assert "UPSTREAM_ECHOED_OK" in out, out


async def test_no_token_no_lifecycle_is_blocked(
    topology: Topology, per_test_cleanup: None
) -> None:
    """No token and no IP registration → no identity → default-deny 403,
    even for a host another tenant has allowlisted."""
    await _bootstrap_rules(topology, allow_host=topology.fake_upstream_name)

    probe = await topology.spawn_probe()  # no token, no lifecycle publish

    rc, out = await probe.exec_sh(
        f"python3 - <<'PY'\n{_connect_script(topology.fake_upstream_name, 443)}\nPY"
    )
    assert rc == 0, out
    assert "PROXY_STATUS='HTTP/1.1 403" in out, out
