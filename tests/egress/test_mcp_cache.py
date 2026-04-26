"""Unit tests for ``rolemesh.egress.mcp_cache`` — gateway-side MCP sync.

These pin the wire-format contract and the dispatch logic from
``apply_snapshot_to_registry`` / ``apply_change_event`` against the
``reverse_proxy._mcp_registry`` it controls. Each case clears the
registry first so leftover state from another test can't paper over a
regression.
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
    )
    assert entry_from_dict(entry_to_dict(entry)) == entry


def test_entry_from_dict_defaults_auth_mode_to_user() -> None:
    # Auth mode is the most likely field to be omitted by an operator
    # crafting a snapshot manually. Explicit default protects the
    # downstream RBAC check.
    entry = entry_from_dict(
        {"name": "x", "url": "https://x", "headers": {}}
    )
    assert entry.auth_mode == "user"


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
        }
    )
    assert entry.headers == {"X-Int": "42", "X-Bool": "True"}


# ---------------------------------------------------------------------------
# apply_snapshot_to_registry
# ---------------------------------------------------------------------------


def test_apply_snapshot_seeds_empty_registry() -> None:
    apply_snapshot_to_registry(
        [
            McpEntry("github", "https://api.github.com", {}, "user"),
            McpEntry("internal", "http://localhost:9100", {"X-Tenant": "t1"}, "service"),
        ]
    )
    reg = reverse_proxy.get_mcp_registry()
    assert set(reg) == {"github", "internal"}
    assert reg["github"][0] == "https://api.github.com"
    assert reg["internal"][2] == "service"


def test_apply_snapshot_drops_stale_names() -> None:
    """If the orchestrator removed an MCP server while the gateway was
    offline, the next snapshot must not leave the dropped name behind.
    Failure mode of the alternative (merge) is "deleted server still
    routable on the gateway after the orchestrator forgot it" —
    a privilege-escalation-grade bug."""
    reverse_proxy.register_mcp_server("stale", "https://stale", {}, "user")
    reverse_proxy.register_mcp_server("kept", "https://kept", {}, "user")

    apply_snapshot_to_registry(
        [McpEntry("kept", "https://kept", {}, "user")]
    )
    assert set(reverse_proxy.get_mcp_registry()) == {"kept"}


def test_apply_snapshot_overwrites_existing_entry() -> None:
    # Same name, different URL — the snapshot wins. Catches the
    # regression where a half-merge leaves the original URL.
    reverse_proxy.register_mcp_server("x", "https://old", {}, "user")
    apply_snapshot_to_registry([McpEntry("x", "https://new", {}, "user")])
    assert reverse_proxy.get_mcp_registry()["x"][0] == "https://new"


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
        }
    )
    assert "github" in reverse_proxy.get_mcp_registry()


def test_change_event_updated_overwrites() -> None:
    reverse_proxy.register_mcp_server("x", "https://old", {}, "user")
    apply_change_event(
        {
            "action": "updated",
            "name": "x",
            "url": "https://new",
            "headers": {},
            "auth_mode": "service",
        }
    )
    url, _, mode = reverse_proxy.get_mcp_registry()["x"]
    assert url == "https://new"
    assert mode == "service"


def test_change_event_deleted_only_needs_name() -> None:
    reverse_proxy.register_mcp_server("x", "https://x", {}, "user")
    apply_change_event({"action": "deleted", "name": "x"})
    assert "x" not in reverse_proxy.get_mcp_registry()


def test_change_event_deleted_unknown_name_is_idempotent() -> None:
    # An at-most-once-loss broadcast may re-deliver the same delete
    # twice. Apply must not raise on the second arrival.
    apply_change_event({"action": "deleted", "name": "ghost"})
    assert reverse_proxy.get_mcp_registry() == {}  # still empty, no exception


def test_change_event_unknown_action_is_dropped() -> None:
    apply_change_event(
        {"action": "renamed", "name": "x", "url": "https://x", "auth_mode": "user"}
    )
    assert reverse_proxy.get_mcp_registry() == {}


def test_change_event_missing_name_is_dropped() -> None:
    # Required field. Missing => skip + log; never raise (handler is
    # behind a NATS callback and an exception there crashes the
    # subscriber loop).
    apply_change_event({"action": "created", "url": "https://x"})
    assert reverse_proxy.get_mcp_registry() == {}


def test_change_event_malformed_url_is_dropped_not_raised() -> None:
    # Missing required field on a created event. The reverse_proxy is
    # protected against half-populated entries because we'd try to
    # forward to ``None``.
    apply_change_event({"action": "created", "name": "x"})
    assert reverse_proxy.get_mcp_registry() == {}
