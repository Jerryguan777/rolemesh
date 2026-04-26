"""Unit tests for the orchestrator-side MCP-registry glue.

Covers the publisher / responder layer added to ``orch_glue.py``:

  * ``publish_mcp_registry_changed`` shapes the wire payload correctly
    for created / updated / deleted actions.
  * ``start_responders`` wires an ``egress.mcp.snapshot.request``
    handler that returns the orchestrator's current view.
  * ``fetch_all_mcp_servers`` reads the in-process registry without
    touching the DB.

The NATS client is a hand-rolled stub (no real NATS dependency)
focused on the publish / subscribe surface ``orch_glue`` uses.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from rolemesh.egress import reverse_proxy
from rolemesh.egress.mcp_cache import (
    MCP_CHANGED_SUBJECT,
    MCP_SNAPSHOT_REQUEST_SUBJECT,
    McpEntry,
)
from rolemesh.egress.orch_glue import (
    fetch_all_mcp_servers,
    publish_mcp_registry_changed,
    start_responders,
)


# ---------------------------------------------------------------------------
# NATS stub
# ---------------------------------------------------------------------------


@dataclass
class _Sub:
    subject: str
    cb: Any  # Callable[[FakeMsg], Awaitable[None]]


class _FakeMsg:
    """Minimal duck-typed NATS message so handlers can ``msg.respond``."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.replies: list[bytes] = []

    async def respond(self, body: bytes) -> None:
        self.replies.append(body)


class FakeNats:
    """Tiny NATS double with the methods orch_glue uses."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subs: list[_Sub] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, data))

    async def subscribe(self, subject: str, cb: Any = None) -> _Sub:
        sub = _Sub(subject=subject, cb=cb)
        self.subs.append(sub)
        return sub


@pytest.fixture
def nc() -> FakeNats:
    return FakeNats()


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    reverse_proxy._mcp_registry.clear()


# ---------------------------------------------------------------------------
# publish_mcp_registry_changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_created_carries_full_entry(nc: FakeNats) -> None:
    entry = McpEntry(
        name="github",
        url="https://api.github.com",
        headers={"X-T": "v"},
        auth_mode="user",
    )
    await publish_mcp_registry_changed(nc, action="created", entry=entry)
    assert len(nc.published) == 1
    subj, body = nc.published[0]
    assert subj == MCP_CHANGED_SUBJECT
    payload = json.loads(body)
    assert payload == {
        "action": "created",
        "name": "github",
        "url": "https://api.github.com",
        "headers": {"X-T": "v"},
        "auth_mode": "user",
    }


@pytest.mark.asyncio
async def test_publish_deleted_only_carries_name(nc: FakeNats) -> None:
    # Deleted events are intentionally minimal — the consumer just
    # needs to know which name to drop, the rest of the row was
    # already removed.
    await publish_mcp_registry_changed(nc, action="deleted", name="github")
    payload = json.loads(nc.published[0][1])
    assert payload == {"action": "deleted", "name": "github"}


@pytest.mark.asyncio
async def test_publish_created_without_entry_is_no_op(nc: FakeNats) -> None:
    # Defensive: publishing a created event without payload would be a
    # caller bug. Don't crash, don't publish a half-formed event.
    await publish_mcp_registry_changed(nc, action="created", entry=None)
    assert nc.published == []


@pytest.mark.asyncio
async def test_publish_deleted_without_name_is_no_op(nc: FakeNats) -> None:
    await publish_mcp_registry_changed(nc, action="deleted", name=None)
    assert nc.published == []


@pytest.mark.asyncio
async def test_publish_swallows_transient_nats_errors() -> None:
    # ``best-effort`` contract from the docstring — a transient NATS
    # outage during a delta publish must not break the orchestrator
    # path that called us.
    class _Boom:
        async def publish(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("nats down")

    await publish_mcp_registry_changed(
        _Boom(),
        action="created",
        entry=McpEntry("x", "https://x", {}, "user"),
    )  # would-raise without the guard


# ---------------------------------------------------------------------------
# fetch_all_mcp_servers — production fetcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_all_mcp_servers_reads_registry() -> None:
    reverse_proxy.register_mcp_server(
        "internal", "http://localhost:9100", {"X-Tenant": "t1"}, "service"
    )
    entries = await fetch_all_mcp_servers()
    assert len(entries) == 1
    assert entries[0] == McpEntry(
        name="internal",
        url="http://localhost:9100",
        headers={"X-Tenant": "t1"},
        auth_mode="service",
    )


@pytest.mark.asyncio
async def test_fetch_all_mcp_servers_returns_empty_when_unregistered() -> None:
    assert await fetch_all_mcp_servers() == []


# ---------------------------------------------------------------------------
# start_responders — MCP snapshot handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_responder_returns_current_registry(nc: FakeNats) -> None:
    reverse_proxy.register_mcp_server("x", "https://x", {}, "user")

    async def _rules_fetcher() -> list[dict[str, Any]]:
        return []

    await start_responders(nc, state=None, rules_fetcher=_rules_fetcher)  # type: ignore[arg-type]

    mcp_subs = [s for s in nc.subs if s.subject == MCP_SNAPSHOT_REQUEST_SUBJECT]
    assert len(mcp_subs) == 1, "MCP snapshot subject must be subscribed"

    msg = _FakeMsg(b"")
    await mcp_subs[0].cb(msg)
    payload = json.loads(msg.replies[0])
    assert payload == {
        "entries": [
            {"name": "x", "url": "https://x", "headers": {}, "auth_mode": "user"}
        ]
    }


@pytest.mark.asyncio
async def test_responder_replies_with_empty_on_fetcher_error(nc: FakeNats) -> None:
    # Fail-soft: a fetcher exception must not leave the gateway hung
    # waiting for a reply. We always send a payload, even if it's
    # ``{"entries": []}``.
    async def _broken_mcp_fetcher() -> list[McpEntry]:
        raise RuntimeError("DB down")

    async def _rules_fetcher() -> list[dict[str, Any]]:
        return []

    await start_responders(
        nc,
        state=None,  # type: ignore[arg-type]
        rules_fetcher=_rules_fetcher,
        mcp_fetcher=_broken_mcp_fetcher,
    )

    mcp_subs = [s for s in nc.subs if s.subject == MCP_SNAPSHOT_REQUEST_SUBJECT]
    msg = _FakeMsg(b"")
    await mcp_subs[0].cb(msg)
    payload = json.loads(msg.replies[0])
    assert payload["entries"] == []
    assert "error" in payload


@pytest.mark.asyncio
async def test_responder_attaches_three_subjects(nc: FakeNats) -> None:
    # Regression guard against a future split that forgets to include
    # the MCP responder. We assert the count + the exact subject set.
    async def _rules_fetcher() -> list[dict[str, Any]]:
        return []

    subs = await start_responders(
        nc, state=None, rules_fetcher=_rules_fetcher  # type: ignore[arg-type]
    )
    assert len(subs) == 3
    subjects = {s.subject for s in nc.subs}
    assert MCP_SNAPSHOT_REQUEST_SUBJECT in subjects
