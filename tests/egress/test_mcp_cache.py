"""Unit tests for ``rolemesh.egress.mcp_cache`` — gateway-side MCP sync.

These pin the wire-format contract and the dispatch logic from
``apply_snapshot_to_registry`` / ``apply_change_event`` against the
``reverse_proxy._mcp_registry`` it controls. Each case clears the
registry first so leftover state from another test can't paper over a
regression.

The registry (and the wire format) is tenant-scoped: entries key on
``(tenant_id, name)``. Old-format payloads without ``tenant_id`` parse
into the "" tenant slot, which no verified identity can match.
"""

from __future__ import annotations

import pytest

from rolemesh.egress import reverse_proxy
from rolemesh.egress.mcp_cache import (
    McpEntry,
    apply_change_event,
    apply_snapshot_to_registry,
    entry_from_dict,
    entry_to_dict,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    # The registry is module-global — wipe it between cases so tests
    # don't observe each other's writes.
    reverse_proxy._mcp_registry.clear()


# ---------------------------------------------------------------------------
# Wire format round-trip
# ---------------------------------------------------------------------------


def test_entry_roundtrip_preserves_all_fields() -> None:
    entry = McpEntry(
        name="github",
        url="https://api.github.com",
        headers={"X-Custom": "value"},
        auth_mode="user",
        tenant_id="t1",
    )
    assert entry_from_dict(entry_to_dict(entry)) == entry


def test_entry_from_dict_defaults_auth_mode_to_user() -> None:
    # Auth mode is the most likely field to be omitted by an operator
    # crafting a snapshot manually. Explicit default protects the
    # downstream RBAC check.
    entry = entry_from_dict(
        {"name": "x", "url": "https://x", "headers": {}, "tenant_id": "t1"}
    )
    assert entry.auth_mode == "user"


def test_entry_from_dict_missing_tenant_parses_as_unreachable_sentinel() -> None:
    # Old wire format (pre-tenancy publisher during a rolling upgrade):
    # parse must not raise, but the entry lands in the "" tenant slot —
    # no verified identity carries an empty tenant, so it can never be
    # served. Fail-closed per request, not a parse error.
    entry = entry_from_dict({"name": "x", "url": "https://x"})
    assert entry.tenant_id == ""


def test_entry_from_dict_normalises_header_keys_and_values_to_str() -> None:
    # Wire payload may carry non-string values if a publisher gets
    # creative. We store as str to avoid surprising the proxy's
    # header-injection path.
    entry = entry_from_dict(
        {
            "name": "x",
            "url": "https://x",
            "headers": {"X-Int": 42, "X-Bool": True},
            "auth_mode": "service",
            "tenant_id": "t1",
        }
    )
    assert entry.headers == {"X-Int": "42", "X-Bool": "True"}


# ---------------------------------------------------------------------------
# apply_snapshot_to_registry
# ---------------------------------------------------------------------------


def test_apply_snapshot_seeds_empty_registry() -> None:
    apply_snapshot_to_registry(
        [
            McpEntry("github", "https://api.github.com", {}, "user", "t1"),
            McpEntry(
                "internal", "http://localhost:9100", {"X-Tenant": "t1"}, "service", "t1"
            ),
        ]
    )
    reg = reverse_proxy.get_mcp_registry()
    assert set(reg) == {("t1", "github"), ("t1", "internal")}
    assert reg[("t1", "github")][0] == "https://api.github.com"
    assert reg[("t1", "internal")][2] == "service"


def test_apply_snapshot_drops_stale_names() -> None:
    """If the orchestrator removed an MCP server while the gateway was
    offline, the next snapshot must not leave the dropped name behind.
    Failure mode of the alternative (merge) is "deleted server still
    routable on the gateway after the orchestrator forgot it" —
    a privilege-escalation-grade bug."""
    reverse_proxy.register_mcp_server("t1", "stale", "https://stale", {}, "user")
    reverse_proxy.register_mcp_server("t1", "kept", "https://kept", {}, "user")

    apply_snapshot_to_registry(
        [McpEntry("kept", "https://kept", {}, "user", "t1")]
    )
    assert set(reverse_proxy.get_mcp_registry()) == {("t1", "kept")}


def test_apply_snapshot_overwrites_existing_entry() -> None:
    # Same (tenant, name), different URL — the snapshot wins. Catches
    # the regression where a half-merge leaves the original URL.
    reverse_proxy.register_mcp_server("t1", "x", "https://old", {}, "user")
    apply_snapshot_to_registry([McpEntry("x", "https://new", {}, "user", "t1")])
    assert reverse_proxy.get_mcp_registry()[("t1", "x")][0] == "https://new"


def test_apply_snapshot_keeps_same_name_across_tenants_distinct() -> None:
    # The bug the composite key fixes: two tenants' same-named servers
    # must coexist, not overwrite each other last-writer-wins.
    apply_snapshot_to_registry(
        [
            McpEntry("jira", "https://a.example.com", {}, "user", "tenant-a"),
            McpEntry("jira", "https://b.example.com", {}, "user", "tenant-b"),
        ]
    )
    reg = reverse_proxy.get_mcp_registry()
    assert reg[("tenant-a", "jira")][0] == "https://a.example.com"
    assert reg[("tenant-b", "jira")][0] == "https://b.example.com"


# ---------------------------------------------------------------------------
# apply_change_event
# ---------------------------------------------------------------------------


def test_change_event_created_inserts() -> None:
    apply_change_event(
        {
            "action": "created",
            "name": "github",
            "url": "https://api.github.com",
            "headers": {},
            "auth_mode": "user",
            "tenant_id": "t1",
        }
    )
    assert ("t1", "github") in reverse_proxy.get_mcp_registry()


def test_change_event_updated_overwrites() -> None:
    reverse_proxy.register_mcp_server("t1", "x", "https://old", {}, "user")
    apply_change_event(
        {
            "action": "updated",
            "name": "x",
            "url": "https://new",
            "headers": {},
            "auth_mode": "service",
            "tenant_id": "t1",
        }
    )
    url, _, mode = reverse_proxy.get_mcp_registry()[("t1", "x")]
    assert url == "https://new"
    assert mode == "service"


def test_change_event_deleted_needs_tenant_and_name() -> None:
    reverse_proxy.register_mcp_server("t1", "x", "https://x", {}, "user")
    apply_change_event({"action": "deleted", "name": "x", "tenant_id": "t1"})
    assert ("t1", "x") not in reverse_proxy.get_mcp_registry()


def test_change_event_deleted_without_tenant_cannot_hit_real_entry() -> None:
    # Old-format delete (no tenant_id) targets the "" slot — it must
    # NOT delete some tenant's same-named entry, or a stale publisher
    # could knock out live routing (or a crafted event could target
    # another tenant's server by name alone).
    reverse_proxy.register_mcp_server("t1", "x", "https://x", {}, "user")
    apply_change_event({"action": "deleted", "name": "x"})
    assert ("t1", "x") in reverse_proxy.get_mcp_registry()


def test_change_event_deleted_scoped_to_its_tenant() -> None:
    reverse_proxy.register_mcp_server("tenant-a", "jira", "https://a", {}, "user")
    reverse_proxy.register_mcp_server("tenant-b", "jira", "https://b", {}, "user")
    apply_change_event({"action": "deleted", "name": "jira", "tenant_id": "tenant-a"})
    reg = reverse_proxy.get_mcp_registry()
    assert ("tenant-a", "jira") not in reg
    assert ("tenant-b", "jira") in reg


def test_change_event_deleted_unknown_name_is_idempotent() -> None:
    # An at-most-once-loss broadcast may re-deliver the same delete
    # twice. Apply must not raise on the second arrival.
    apply_change_event({"action": "deleted", "name": "ghost", "tenant_id": "t1"})
    assert reverse_proxy.get_mcp_registry() == {}  # still empty, no exception


def test_change_event_unknown_action_is_dropped() -> None:
    apply_change_event(
        {
            "action": "renamed",
            "name": "x",
            "url": "https://x",
            "auth_mode": "user",
            "tenant_id": "t1",
        }
    )
    assert reverse_proxy.get_mcp_registry() == {}


def test_change_event_missing_name_is_dropped() -> None:
    # Required field. Missing => skip + log; never raise (handler is
    # behind a NATS callback and an exception there crashes the
    # subscriber loop).
    apply_change_event({"action": "created", "url": "https://x", "tenant_id": "t1"})
    assert reverse_proxy.get_mcp_registry() == {}


def test_change_event_malformed_url_is_dropped_not_raised() -> None:
    # Missing required field on a created event. The reverse_proxy is
    # protected against half-populated entries because we'd try to
    # forward to ``None``.
    apply_change_event({"action": "created", "name": "x", "tenant_id": "t1"})
    assert reverse_proxy.get_mcp_registry() == {}
