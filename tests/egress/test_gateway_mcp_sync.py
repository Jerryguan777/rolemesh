"""Gateway MCP registry sync: cold-start convergence + reconcile.

The contract under test (mirrors test_gateway_degraded_startup for the
rule side): the gateway serves immediately; a background task retries
the MCP snapshot until the orchestrator's responder appears — the
regression test for the incident where a lost cold-start snapshot left
the registry empty for 22 days — then reconciles the registry against
the orchestrator every _RECONCILE_INTERVAL_S; the change-delta
subscription opens before the first snapshot fetch; and /healthz
exposes the MCP seed state without ever leaving 200.

Only the NATS boundary is faked (_ScriptedMcpNats). The registry,
apply_snapshot_to_registry, the subscription wiring, and the aiohttp
/healthz endpoint are all real.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

import rolemesh.egress.gateway as gateway
from rolemesh.egress import reverse_proxy
from rolemesh.egress.mcp_cache import (
    MCP_CHANGED_SUBJECT,
    MCP_SNAPSHOT_REQUEST_SUBJECT,
)
from rolemesh.egress.reverse_proxy import start_credential_proxy

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    # The registry is module-global — wipe it between cases so tests
    # don't observe each other's writes.
    reverse_proxy._mcp_registry.clear()


# ---------------------------------------------------------------------------
# NATS-boundary fake (the only mock in this module)
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Sub:
    async def unsubscribe(self) -> None:
        return None


def _entry(name: str, url: str) -> dict[str, Any]:
    return {"name": name, "url": url, "headers": {}, "auth_mode": "user"}


class _ScriptedMcpNats:
    """NATS client fake: ``request`` fails *failures* times, then
    answers with *snapshot_entries* (mutable — tests may change it
    between reconcile cycles, and ``fail_next`` injects transient
    failures after the seed); ``events`` records every subscribe and
    request in arrival order for the ordering assertion."""

    def __init__(
        self,
        *,
        snapshot_entries: list[dict[str, Any]] | None = None,
        failures: int = 0,
    ) -> None:
        self.snapshot_entries = snapshot_entries or []
        self.failures = failures
        self.fail_next = 0
        self.request_count = 0
        self.events: list[tuple[str, str]] = []
        self.handlers: dict[str, Any] = {}

    async def request(self, subject: str, payload: bytes, timeout: float) -> _Msg:
        self.events.append(("request", subject))
        self.request_count += 1
        if self.request_count <= self.failures or self.fail_next > 0:
            if self.fail_next > 0:
                self.fail_next -= 1
            raise TimeoutError("no snapshot responder")
        return _Msg(json.dumps({"entries": self.snapshot_entries}).encode())

    async def subscribe(self, subject: str, cb: Any) -> _Sub:
        self.events.append(("subscribe", subject))
        self.handlers[subject] = cb
        return _Sub()


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


# ---------------------------------------------------------------------------
# cold-start race: retry until the responder appears (22-day incident)
# ---------------------------------------------------------------------------


async def test_cold_start_race_converges_when_responder_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway up before the orchestrator, snapshot RPC timing out. The
    old one-shot fetch left the registry empty until a manual restart —
    pre-existing MCP servers never emit deltas, so nothing refilled it.
    The seed task must keep retrying and converge once the responder
    answers."""
    monkeypatch.setattr(gateway, "_SNAPSHOT_RETRY_INITIAL_S", 0.01)
    monkeypatch.setattr(gateway, "_SNAPSHOT_RETRY_MAX_S", 0.04)

    nc = _ScriptedMcpNats(
        snapshot_entries=[_entry("github", "https://api.github.com")],
        failures=3,
    )
    async with AsyncExitStack() as stack:
        state = await gateway._start_mcp_sync(nc, stack)  # type: ignore[arg-type]

        # Degraded window: attempts are happening, nothing is seeded.
        await _wait_for(lambda: nc.request_count >= 1)
        assert not state.seeded
        assert reverse_proxy.get_mcp_registry() == {}

        # Responder "appears" after the scripted failures; the retry
        # loop must land the snapshot on the next attempt.
        await _wait_for(lambda: state.seeded)
        assert nc.request_count == 4, "3 failures + 1 success"
        assert set(reverse_proxy.get_mcp_registry()) == {"github"}


async def test_seed_success_with_empty_snapshot_still_counts_as_seeded() -> None:
    """An orchestrator with zero MCP servers is a legal steady state:
    seeded must mean "the RPC succeeded", never "the registry is
    non-empty"."""
    nc = _ScriptedMcpNats(snapshot_entries=[])
    async with AsyncExitStack() as stack:
        state = await gateway._start_mcp_sync(nc, stack)  # type: ignore[arg-type]
        await _wait_for(lambda: state.seeded)
        assert reverse_proxy.get_mcp_registry() == {}


# ---------------------------------------------------------------------------
# periodic reconcile: drift self-heals within one interval
# ---------------------------------------------------------------------------


async def test_reconcile_heals_registry_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delta lost to at-most-once NATS shows up as registry drift.
    Simulate it by mutating the registry behind the sync's back and
    assert the next reconcile cycle restores snapshot state — both a
    dropped entry and a diverged URL."""
    monkeypatch.setattr(gateway, "_RECONCILE_INTERVAL_S", 0.02)

    nc = _ScriptedMcpNats(
        snapshot_entries=[
            _entry("github", "https://api.github.com"),
            _entry("jira", "https://jira.internal"),
        ]
    )
    async with AsyncExitStack() as stack:
        state = await gateway._start_mcp_sync(nc, stack)  # type: ignore[arg-type]
        await _wait_for(lambda: state.seeded)

        # Drift: one entry vanished, one points at the wrong origin.
        reverse_proxy.unregister_mcp_server("github")
        reverse_proxy.register_mcp_server("jira", "https://wrong", {}, "user")

        def _healed() -> bool:
            reg = reverse_proxy.get_mcp_registry()
            return set(reg) == {"github", "jira"} and reg["jira"][0] == "https://jira.internal"

        await _wait_for(_healed)


async def test_reconcile_failure_keeps_task_alive_until_next_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconcile fetch failure (orchestrator restarting) must not kill
    the task, un-seed the state, or fall back to fast backoff; the next
    cycle picks the snapshot up again."""
    monkeypatch.setattr(gateway, "_RECONCILE_INTERVAL_S", 0.02)

    nc = _ScriptedMcpNats(snapshot_entries=[_entry("github", "https://api.github.com")])
    async with AsyncExitStack() as stack:
        state = await gateway._start_mcp_sync(nc, stack)  # type: ignore[arg-type]
        await _wait_for(lambda: state.seeded)

        nc.fail_next = 1  # one orchestrator hiccup
        reverse_proxy.unregister_mcp_server("github")  # drift during the hiccup

        await _wait_for(lambda: "github" in reverse_proxy.get_mcp_registry())
        assert state.seeded


# ---------------------------------------------------------------------------
# ordering: subscription opens before the first snapshot fetch
# ---------------------------------------------------------------------------


async def test_subscription_opens_before_first_snapshot_fetch() -> None:
    """No delta may fall between snapshot generation and subscription:
    _start_mcp_sync must subscribe to the change subject before the
    seed task's first fetch hits the wire."""
    nc = _ScriptedMcpNats(snapshot_entries=[])
    async with AsyncExitStack() as stack:
        state = await gateway._start_mcp_sync(nc, stack)  # type: ignore[arg-type]
        await _wait_for(lambda: state.seeded)

    sub_idx = nc.events.index(("subscribe", MCP_CHANGED_SUBJECT))
    req_idx = nc.events.index(("request", MCP_SNAPSHOT_REQUEST_SUBJECT))
    assert sub_idx < req_idx


# ---------------------------------------------------------------------------
# healthz: both seed flags, entry count, always 200
# ---------------------------------------------------------------------------


class _UnusedCredResolver:
    """healthz never touches credentials; resolving here is a test bug."""

    async def resolve(self, tenant_id: str, provider: str) -> dict[str, Any]:
        raise AssertionError("credential resolver must not be hit by /healthz")


async def test_healthz_combines_both_seed_flags_and_stays_200() -> None:
    """status must be "ok" iff BOTH planes are seeded; each flag is
    reported independently; mcp_entries tracks the registry; and the
    HTTP status never leaves 200 (a probe restarting a gateway that is
    merely waiting for the orchestrator recreates the startup
    deadlock)."""
    flags = {"rules": False, "mcp": False}
    async with AsyncExitStack() as stack:
        runner = await start_credential_proxy(
            0,
            credential_resolver=_UnusedCredResolver(),  # type: ignore[arg-type]
            rules_seeded=lambda: flags["rules"],
            mcp_seeded=lambda: flags["mcp"],
        )
        stack.push_async_callback(runner.cleanup)
        client = TestClient(TestServer(runner.app))
        await client.start_server()
        stack.push_async_callback(client.close)

        async def _body() -> dict[str, Any]:
            resp = await client.get("/healthz")
            assert resp.status == 200
            return await resp.json()

        assert await _body() == {
            "status": "degraded",
            "rules_seeded": False,
            "mcp_seeded": False,
            "mcp_entries": 0,
        }

        flags["rules"] = True
        assert await _body() == {
            "status": "degraded",
            "rules_seeded": True,
            "mcp_seeded": False,
            "mcp_entries": 0,
        }

        flags["rules"], flags["mcp"] = False, True
        assert await _body() == {
            "status": "degraded",
            "rules_seeded": False,
            "mcp_seeded": True,
            "mcp_entries": 0,
        }

        # Both seeded with an EMPTY registry is "ok" — no MCP servers
        # configured is a legal state, not a degradation.
        flags["rules"] = True
        assert await _body() == {
            "status": "ok",
            "rules_seeded": True,
            "mcp_seeded": True,
            "mcp_entries": 0,
        }

        reverse_proxy.register_mcp_server("github", "https://api.github.com", {}, "user")
        assert await _body() == {
            "status": "ok",
            "rules_seeded": True,
            "mcp_seeded": True,
            "mcp_entries": 1,
        }
