"""Gateway degraded startup (docs/21-container-runtime-decoupling §5).

The contract under test: the gateway serves immediately after NATS is
up; until the rule snapshot is seeded the policy plane is
deterministically deny-all and /healthz reports degraded (but stays
200); a background task retries the snapshot and seeding flips the
same request from block to allow.

Only the NATS boundary is faked (_ScriptedNats). PolicyCache,
EgressSafetyCaller, the real ``egress.domain_rule`` check, the
subscription handler, and the aiohttp /healthz endpoint are all real.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

import rolemesh.egress.gateway as gateway
from rolemesh.egress.policy_cache import (
    PolicyCache,
    subscribe_rule_changes,
)
from rolemesh.egress.reverse_proxy import start_credential_proxy
from rolemesh.egress.safety_call import (
    AuditPublisher,
    EgressRequest,
    EgressSafetyCaller,
)
from rolemesh.egress.token_identity import Identity
from rolemesh.safety.checks.egress_domain_rule import make_egress_domain_check

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# NATS-boundary fake (the only mock in this module)
# ---------------------------------------------------------------------------


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Sub:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _ScriptedNats:
    """NATS client fake: ``request`` fails *failures* times, then answers
    with *snapshot_rules*; ``subscribe`` captures the callback so tests
    can inject rule-change events through the real handler; ``publish``
    swallows audit traffic."""

    def __init__(
        self,
        *,
        snapshot_rules: list[dict[str, Any]] | None = None,
        failures: int = 0,
    ) -> None:
        self.snapshot_rules = snapshot_rules or []
        self.failures = failures
        self.request_count = 0
        self.handlers: dict[str, Any] = {}

    async def request(self, subject: str, payload: bytes, timeout: float) -> _Msg:
        self.request_count += 1
        if self.request_count <= self.failures:
            raise TimeoutError("no snapshot responder yet")
        return _Msg(json.dumps({"rules": self.snapshot_rules}).encode())

    async def subscribe(self, subject: str, cb: Any) -> _Sub:
        self.handlers[subject] = cb
        return _Sub()

    async def publish(self, subject: str, data: bytes) -> None:
        return None

    async def emit_rule_change(self, event: dict[str, Any]) -> None:
        """Drive the real subscription handler as NATS delivery would."""
        await self.handlers["safety.rule.changed"](_Msg(json.dumps(event).encode()))


# ---------------------------------------------------------------------------
# Shared real-component fixtures
# ---------------------------------------------------------------------------


def _identity() -> Identity:
    return Identity(
        tenant_id="tenant-a",
        coworker_id="coworker-x",
        user_id="user-1",
        conversation_id="conv-1",
        job_id="job-1",
        container_name="rolemesh-x-1",
    )


def _allow_rule(rule_id: str = "r-allow", pattern: str = "api.anthropic.com") -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "tenant_id": "tenant-a",
        "coworker_id": "coworker-x",
        "stage": "egress_request",
        "check_id": "egress.domain_rule",
        "config": {"domain_patterns": [pattern]},
        "priority": 100,
        "enabled": True,
    }


def _caller(cache: PolicyCache, nc: _ScriptedNats) -> EgressSafetyCaller:
    return EgressSafetyCaller(
        cache=cache,
        checks={"egress.domain_rule": make_egress_domain_check()},
        audit_publisher=AuditPublisher(nats_client=nc),  # type: ignore[arg-type]
    )


_REQUEST = EgressRequest(host="api.anthropic.com", port=443, mode="forward")


class _UnusedCredResolver:
    """healthz never touches credentials; resolving here is a test bug."""

    async def resolve(self, tenant_id: str, provider: str) -> dict[str, Any]:
        raise AssertionError("credential resolver must not be hit by /healthz")


async def _healthz_client(cache: PolicyCache, stack: AsyncExitStack) -> TestClient:
    runner = await start_credential_proxy(
        0,
        credential_resolver=_UnusedCredResolver(),  # type: ignore[arg-type]
        rules_seeded=lambda: cache.seeded,
    )
    stack.push_async_callback(runner.cleanup)
    client = TestClient(TestServer(runner.app))
    await client.start_server()
    stack.push_async_callback(client.close)
    return client


# ---------------------------------------------------------------------------
# healthz: degraded indicator, always 200
# ---------------------------------------------------------------------------


async def test_healthz_is_200_and_degraded_while_snapshot_missing() -> None:
    """No snapshot responder: the gateway must still answer 200 (so the
    orchestrator's readiness probe passes — restart-looping here would
    recreate the startup deadlock) while flagging itself degraded."""
    cache = PolicyCache()
    async with AsyncExitStack() as stack:
        client = await _healthz_client(cache, stack)
        resp = await client.get("/healthz")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "degraded", "rules_seeded": False}


async def test_healthz_reports_ok_once_snapshot_seeded() -> None:
    cache = PolicyCache()
    async with AsyncExitStack() as stack:
        client = await _healthz_client(cache, stack)
        await cache.seed([])  # empty-but-authoritative snapshot counts
        resp = await client.get("/healthz")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "ok", "rules_seeded": True}


async def test_healthz_without_seeded_callable_reports_ok() -> None:
    """Host-side deployments that don't wire ``rules_seeded`` keep the
    old always-healthy behavior."""
    async with AsyncExitStack() as stack:
        runner = await start_credential_proxy(
            0,
            credential_resolver=_UnusedCredResolver(),  # type: ignore[arg-type]
        )
        stack.push_async_callback(runner.cleanup)
        client = TestClient(TestServer(runner.app))
        await client.start_server()
        stack.push_async_callback(client.close)
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"


# ---------------------------------------------------------------------------
# default-deny before seed (the security invariant)
# ---------------------------------------------------------------------------


async def test_unseeded_gateway_denies_request_that_rules_would_allow() -> None:
    """The deny window must be deterministic, not the accident of an
    empty cache: even when a rule-change event already inserted a
    matching allow rule during the degraded window, requests stay
    blocked until the authoritative snapshot lands.

    This is the test that fails if someone removes the ``seeded`` gate
    and relies on get_rules_for returning []."""
    nc = _ScriptedNats()
    cache = PolicyCache()
    await subscribe_rule_changes(nc, cache)  # type: ignore[arg-type]
    await nc.emit_rule_change({"action": "created", **_allow_rule()})

    decision = await _caller(cache, nc).decide(identity=_identity(), request=_REQUEST)

    assert decision.action == "block"
    assert "not yet loaded" in decision.reason


# ---------------------------------------------------------------------------
# background retry: block -> seed -> allow
# ---------------------------------------------------------------------------


async def test_snapshot_retry_flips_same_request_from_block_to_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responder absent for the first attempts, then comes up: the retry
    task must keep trying, seed on success, and the identical request
    that was blocked during the window is then allowed by the rules."""
    monkeypatch.setattr(gateway, "_SNAPSHOT_RETRY_INITIAL_S", 0.01)
    monkeypatch.setattr(gateway, "_SNAPSHOT_RETRY_MAX_S", 0.04)

    nc = _ScriptedNats(snapshot_rules=[_allow_rule()], failures=3)
    cache = PolicyCache()
    await subscribe_rule_changes(nc, cache)  # type: ignore[arg-type]
    caller = _caller(cache, nc)

    seed_task = asyncio.create_task(gateway._seed_rules_with_retry(nc, cache))  # type: ignore[arg-type]

    before = await caller.decide(identity=_identity(), request=_REQUEST)
    assert before.action == "block"

    await asyncio.wait_for(seed_task, timeout=5.0)
    assert nc.request_count == 4, "3 failures + 1 success"
    assert cache.seeded

    after = await caller.decide(identity=_identity(), request=_REQUEST)
    assert after.action == "allow"
    assert after.triggered_rule_ids == ["r-allow"]


async def test_retry_backoff_doubles_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff schedule: 1, 2, 4, ... capped at the max constant. The
    clock is the one boundary mocked here."""
    monkeypatch.setattr(gateway, "_SNAPSHOT_RETRY_INITIAL_S", 1.0)
    monkeypatch.setattr(gateway, "_SNAPSHOT_RETRY_MAX_S", 4.0)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(gateway.asyncio, "sleep", _recording_sleep)

    nc = _ScriptedNats(snapshot_rules=[], failures=6)
    cache = PolicyCache()
    await asyncio.wait_for(gateway._seed_rules_with_retry(nc, cache), timeout=5.0)  # type: ignore[arg-type]

    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]
    assert cache.seeded


async def test_seed_task_is_cancelled_cleanly_by_exit_stack() -> None:
    """Shutdown while still degraded (responder never appeared) must not
    leak the retry task or raise out of the exit stack."""

    class _NeverAnswers(_ScriptedNats):
        async def request(self, subject: str, payload: bytes, timeout: float) -> _Msg:
            await asyncio.Event().wait()  # hang like a lost RPC
            raise AssertionError("unreachable")

    nc = _NeverAnswers()
    cache = PolicyCache()
    async with AsyncExitStack() as stack:
        task = asyncio.create_task(gateway._seed_rules_with_retry(nc, cache))  # type: ignore[arg-type]
        stack.push_async_callback(gateway._cancel_task, task)
        await asyncio.sleep(0)  # let the task start its request

    assert task.cancelled()
    assert not cache.seeded


# ---------------------------------------------------------------------------
# event-before-seed ordering (no event gap, seed is authoritative)
# ---------------------------------------------------------------------------


async def test_rule_change_during_degraded_window_is_superseded_by_seed() -> None:
    """Subscription opens before the seed. An event that arrives in the
    degraded window but is NOT in the snapshot must vanish on seed (the
    snapshot is the authority); rules in the snapshot take effect; and
    events after the seed apply incrementally on top."""
    nc = _ScriptedNats(snapshot_rules=[_allow_rule("r-snap", "api.anthropic.com")])
    cache = PolicyCache()
    await subscribe_rule_changes(nc, cache)  # type: ignore[arg-type]
    caller = _caller(cache, nc)

    # Degraded-window event for a rule the snapshot does not contain.
    await nc.emit_rule_change(
        {"action": "created", **_allow_rule("r-stale", "stale.example.com")}
    )

    snapshot = await gateway.fetch_snapshot_via_nats(nc)  # type: ignore[arg-type]
    await cache.seed(snapshot)

    stale = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="stale.example.com", port=443, mode="forward"),
    )
    assert stale.action == "block", "rule outside the authoritative snapshot must not survive seed"

    snap = await caller.decide(identity=_identity(), request=_REQUEST)
    assert snap.action == "allow"
    assert snap.triggered_rule_ids == ["r-snap"]

    # Post-seed events still apply incrementally.
    await nc.emit_rule_change(
        {"action": "created", **_allow_rule("r-live", "live.example.com")}
    )
    live = await caller.decide(
        identity=_identity(),
        request=EgressRequest(host="live.example.com", port=443, mode="forward"),
    )
    assert live.action == "allow"
