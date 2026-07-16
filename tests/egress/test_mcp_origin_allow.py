"""Registry-managed MCP origin allow layer (feat/egress-allow-registered-mcp-origins).

Adding an MCP server through the admin API must be sufficient for the
gateway to permit egress to its origin on the REVERSE (credential proxy)
path — no hand-written ``egress.domain_rule`` required. The properties
under test:

  - a registered origin is allowed on the reverse path, with its own
    audit finding code;
  - the SAME host:port stays default-denied on the forward and DNS
    paths (those hosts are agent-controlled);
  - the allow set follows the registry live: URL edits drop the old
    origin immediately, deletes drop the origin entirely;
  - the layer does not depend on the tenant-rule snapshot (an MCP call
    works during the rules-degraded window).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from rolemesh.egress import reverse_proxy
from rolemesh.egress.policy_cache import PolicyCache
from rolemesh.egress.reverse_proxy import (
    is_registered_mcp_origin,
    register_mcp_server,
    registered_mcp_origins,
    unregister_mcp_server,
)
from rolemesh.egress.safety_call import (
    AuditPublisher,
    EgressRequest,
    EgressSafetyCaller,
)
from rolemesh.egress.token_identity import Identity

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot + restore the module-global MCP registry around each test."""
    saved = reverse_proxy.get_mcp_registry()
    for tenant_id, name in list(saved):
        unregister_mcp_server(tenant_id, name)
    yield
    for tenant_id, name in list(reverse_proxy.get_mcp_registry()):
        unregister_mcp_server(tenant_id, name)
    for (tenant_id, name), (url, headers, auth_mode) in saved.items():
        register_mcp_server(tenant_id, name, url, headers, auth_mode)


@dataclass
class _FakeNats:
    published: list[tuple[str, dict[str, Any]]]

    async def publish(self, subject: str, data: bytes) -> None:
        import json

        self.published.append((subject, json.loads(data)))


def _identity() -> Identity:
    return Identity(
        tenant_id="tenant-a",
        coworker_id="coworker-x",
        user_id="user-1",
        conversation_id="conv-1",
        job_id="job-1",
        container_name="rolemesh-foo-1",
    )


async def _make_caller(*, nc: _FakeNats, seeded: bool = True) -> EgressSafetyCaller:
    """Caller with zero tenant rules, wired the way the gateway wires it."""
    cache = PolicyCache()
    if seeded:
        await cache.seed([])
    return EgressSafetyCaller(
        cache=cache,
        checks={},
        audit_publisher=AuditPublisher(nats_client=nc),  # type: ignore[arg-type]
        mcp_allow=is_registered_mcp_origin,
    )


async def _drain_audit(nc: _FakeNats) -> None:
    for _ in range(50):
        await asyncio.sleep(0.02)
        if nc.published:
            return


# ---------------------------------------------------------------------------
# Origin derivation
# ---------------------------------------------------------------------------


async def test_registered_origins_default_ports() -> None:
    """Scheme-default ports match the ports the proxy actually dials."""
    register_mcp_server("tenant-a", "a", "https://mcp.example.com")
    register_mcp_server("tenant-a", "b", "http://intranet.local")
    register_mcp_server("tenant-a", "c", "http://host.docker.internal:9100")
    assert registered_mcp_origins("tenant-a") == {
        ("mcp.example.com", 443),
        ("intranet.local", 80),
        ("host.docker.internal", 9100),
    }


async def test_predicate_normalises_host() -> None:
    """Case and trailing-dot differences must not defeat the match."""
    register_mcp_server("tenant-a", "a", "https://mcp.example.com")
    assert is_registered_mcp_origin("tenant-a", "MCP.Example.COM.", 443)
    assert not is_registered_mcp_origin("tenant-a", "mcp.example.com", 8443)
    assert not is_registered_mcp_origin("tenant-a", "evil-mcp.example.com", 443)


# ---------------------------------------------------------------------------
# Decision layer: reverse allowed, forward/DNS unaffected
# ---------------------------------------------------------------------------


async def test_registered_origin_allowed_on_reverse() -> None:
    """The user-facing fix: adding an MCP server is enough for its calls
    to pass the egress gate — no separate egress.domain_rule needed."""
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "allow"
    assert decision.reason == "Registered MCP server origin"
    assert decision.findings[0]["code"] == "EGRESS.MCP_REGISTRY_ALLOWED"


async def test_unregistered_origin_still_blocked_on_reverse() -> None:
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "block"
    assert "No egress allowlist rule matched" in decision.reason


async def test_other_tenants_origin_not_allowed() -> None:
    """Tenant isolation: an origin tenant-b registered must NOT grant
    egress to tenant-a's coworkers — the allow layer is keyed on the
    verified identity's tenant, same as the routing lookup."""
    register_mcp_server("tenant-b", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    decision = await caller.decide(
        identity=_identity(),  # tenant-a
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "block"


async def test_mcp_allow_does_not_apply_to_forward_proxy() -> None:
    """Security boundary: the forward CONNECT target is agent-controlled,
    so a registered MCP host must NOT be reachable that way — only via
    the /mcp-proxy route whose upstream is server-derived."""
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(
            host="mcp.example.com", port=9100, mode="forward", method="CONNECT"
        ),
    )
    assert decision.action == "block"


async def test_mcp_allow_does_not_apply_to_dns() -> None:
    """Same boundary for the DNS plane: the queried name is agent-controlled."""
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(
            host="mcp.example.com", port=9100, mode="dns", qtype="A"
        ),
    )
    assert decision.action == "block"


# ---------------------------------------------------------------------------
# Hot-reload semantics
# ---------------------------------------------------------------------------


async def test_url_edit_drops_old_origin_and_allows_new() -> None:
    """Editing a server's URL (same registry semantics as the
    egress.mcp.changed hot-reload) must revoke the old origin at once."""
    register_mcp_server("tenant-a", "jira", "http://old.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)

    register_mcp_server("tenant-a", "jira", "http://new.example.com:9200")

    old = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="old.example.com", port=9100, mode="reverse"),
    )
    new = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="new.example.com", port=9200, mode="reverse"),
    )
    assert old.action == "block"
    assert new.action == "allow"


async def test_delete_revokes_origin() -> None:
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    unregister_mcp_server("tenant-a", "jira")
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "block"


async def test_shared_origin_survives_deleting_one_server() -> None:
    """Two servers on one origin: deleting one must not revoke the other."""
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    register_mcp_server("tenant-a", "wiki", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    unregister_mcp_server("tenant-a", "jira")
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "allow"


# ---------------------------------------------------------------------------
# Degraded startup + audit
# ---------------------------------------------------------------------------


async def test_mcp_allow_works_during_rules_degraded_window() -> None:
    """MCP egress depends on the MCP registry snapshot, not the tenant-rule
    snapshot — a registered origin is allowed even before rules seed."""
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc, seeded=False)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "allow"


async def test_empty_registry_blocks_during_degraded_window() -> None:
    """Fail-closed before the MCP snapshot lands: nothing registered →
    the predicate cannot match and the rules gate still denies."""
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc, seeded=False)
    decision = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    assert decision.action == "block"


async def test_mcp_allow_decision_is_audited() -> None:
    register_mcp_server("tenant-a", "jira", "http://mcp.example.com:9100")
    nc = _FakeNats(published=[])
    caller = await _make_caller(nc=nc)
    await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="mcp.example.com", port=9100, mode="reverse"),
    )
    await _drain_audit(nc)
    assert nc.published, "MCP-allow decision was not audited"
    _, payload = nc.published[-1]
    assert payload["verdict_action"] == "allow"
    assert payload["findings"][0]["code"] == "EGRESS.MCP_REGISTRY_ALLOWED"
