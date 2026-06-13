"""Tests for rolemesh.container.network.

Only ``agent_facing_nats_url`` survives in this module: bridge
creation and the probe-container reachability checks were retired by
the declarative-infrastructure refactor (compose declares the
topology; ``DockerRuntime.verify_infrastructure`` checks it — see
test_verify_infrastructure.py).
"""

from __future__ import annotations

from rolemesh.container.network import agent_facing_nats_url

# ---------------------------------------------------------------------------
# agent_facing_nats_url — single source of truth for the bridge rewrite
# ---------------------------------------------------------------------------


def test_agent_facing_nats_url_rewrites_loopback_forms() -> None:
    assert agent_facing_nats_url("nats://localhost:4222") == "nats://nats:4222"
    assert agent_facing_nats_url("nats://127.0.0.1:4222") == "nats://nats:4222"


def test_agent_facing_nats_url_leaves_non_loopback_untouched() -> None:
    # An operator who already points NATS_URL at a real host/alias should
    # not have it rewritten out from under them.
    assert agent_facing_nats_url("nats://nats.prod:4222") == "nats://nats.prod:4222"
    assert agent_facing_nats_url("nats://10.0.0.5:4222") == "nats://10.0.0.5:4222"
