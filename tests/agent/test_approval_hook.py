"""Adversarial tests for the container-side blocking approval hook (S2).

These drive ``ApprovalHookHandler`` against a *stub orchestrator*: a broker
that records published subjects/payloads and a hand-driven
``resolve_decision``. No NATS, no DB — the handler's NATS/DB wiring lives in
``agent_runner.main`` and is exercised separately.

The bugs these target (not a mirror of the implementation):
  * a non-MCP tool, or an MCP tool with no matching policy, must NOT block
    or publish anything (a regression here would freeze every tool call);
  * a reject / timeout must return a *block* verdict, never silently allow;
  * concurrent approvals in one turn must route back independently
    (cross-wiring would approve the wrong call);
  * the ``finally`` must emit ``approval_cancel`` on reject / timeout / Stop
    (CancelledError) / exception — and must NOT on a clean approve;
  * a decision that arrives after timeout (or twice) must be a no-op
    (first-wins), or a late click would resurrect a dead call.

Each test runs its own event loop via ``asyncio.run`` so the suite does not
depend on a pytest-asyncio mode being configured.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_runner.approval.policy import ApprovalPolicy
from agent_runner.hooks.events import ToolCallEvent, ToolCallVerdict
from agent_runner.hooks.handlers.approval import (
    ApprovalHookHandler,
    parse_mcp_tool_name,
)

_FIXED_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


class StubBroker:
    """Records every publish so tests can assert on the wire traffic."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, payload: dict[str, Any]) -> None:
        self.published.append((subject, payload))

    def by_leaf(self, leaf: str) -> list[dict[str, Any]]:
        return [p for s, p in self.published if s.endswith(f".{leaf}")]

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self.by_leaf("approval_request")

    @property
    def cancels(self) -> list[dict[str, Any]]:
        return self.by_leaf("approval_cancel")


def _policy(
    *,
    server: str = "email",
    tool: str = "send",
    condition: dict[str, Any] | None = None,
    priority: int = 0,
    enabled: bool = True,
    pid: str = "11111111-1111-1111-1111-111111111111",
) -> ApprovalPolicy:
    return ApprovalPolicy(
        id=pid,
        tenant_id="tenant-1",
        mcp_server_name=server,
        tool_name=tool,
        condition_expr=condition if condition is not None else {"always": True},
        enabled=enabled,
        priority=priority,
        updated_at=_FIXED_NOW,
    )


def _handler(
    broker: StubBroker,
    policies: list[ApprovalPolicy],
    *,
    timeout_ms: int = 300_000,
    user_id: str | None = "user-1",
    **kw: Any,
) -> ApprovalHookHandler:
    return ApprovalHookHandler(
        publish=broker.publish,
        policies=policies,
        job_id="job-1",
        tenant_id="tenant-1",
        coworker_id="cow-1",
        conversation_id="conv-1",
        user_id=user_id,
        timeout_ms=timeout_ms,
        **kw,
    )


def _event(tool_name: str, params: dict[str, Any] | None = None) -> ToolCallEvent:
    return ToolCallEvent(tool_name=tool_name, tool_input=params or {})


async def _wait_for_requests(broker: StubBroker, n: int) -> None:
    """Yield until ``broker`` has at least ``n`` approval_request publishes."""
    for _ in range(10_000):
        if len(broker.requests) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {n} approval_request(s), got {len(broker.requests)}")


# ---------------------------------------------------------------------------
# parse_mcp_tool_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__email__send", ("email", "send")),
        ("mcp__email__send__v2", ("email", "send__v2")),  # tool keeps inner __
        ("Bash", None),
        ("Read", None),
        ("", None),
        ("mcp__", None),
        ("mcp__email", None),       # no tool component
        ("mcp__email__", None),     # empty tool
        ("mcp____send", None),      # empty server
    ],
)
def test_parse_mcp_tool_name(name: str, expected: tuple[str, str] | None) -> None:
    assert parse_mcp_tool_name(name) == expected


# ---------------------------------------------------------------------------
# allow paths — must not block, must not publish
# ---------------------------------------------------------------------------


def test_non_mcp_tool_allows_without_publishing() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])
    verdict = asyncio.run(handler.on_pre_tool_use(_event("Bash", {"command": "ls"})))
    assert verdict is None
    assert broker.published == []


def test_mcp_tool_without_matching_policy_allows() -> None:
    broker = StubBroker()
    # Policy is for a different server; this call must sail through.
    handler = _handler(broker, [_policy(server="github")])
    verdict = asyncio.run(handler.on_pre_tool_use(_event("mcp__email__send")))
    assert verdict is None
    assert broker.published == []


def test_empty_policy_snapshot_allows_all_mcp() -> None:
    broker = StubBroker()
    handler = _handler(broker, [])
    verdict = asyncio.run(handler.on_pre_tool_use(_event("mcp__email__send")))
    assert verdict is None
    assert broker.published == []


# ---------------------------------------------------------------------------
# approve / reject / timeout
# ---------------------------------------------------------------------------


def test_approve_returns_none_and_does_not_cancel() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])

    async def go() -> ToolCallVerdict | None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        assert handler.resolve_decision({"request_id": rid, "decision": "approve"}) is True
        return await task

    verdict = asyncio.run(go())
    assert verdict is None              # tool is allowed to execute in-band
    assert len(broker.requests) == 1
    assert broker.cancels == []          # approve does NOT emit cancel (§3.3)


def test_reject_blocks_and_emits_cancel() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])

    async def go() -> ToolCallVerdict | None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        handler.resolve_decision(
            {"request_id": rid, "decision": "reject", "note": "too risky"}
        )
        return await task

    verdict = asyncio.run(go())
    assert isinstance(verdict, ToolCallVerdict)
    assert verdict.block is True
    assert verdict.reason is not None and "rejected" in verdict.reason
    assert "too risky" in verdict.reason          # approver note surfaced
    assert len(broker.cancels) == 1
    assert broker.cancels[0]["request_id"] == broker.requests[0]["request_id"]


def test_timeout_blocks_and_emits_cancel() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()], timeout_ms=20)
    # Never resolved → wait_for fires.
    verdict = asyncio.run(handler.on_pre_tool_use(_event("mcp__email__send")))
    assert isinstance(verdict, ToolCallVerdict)
    assert verdict.block is True
    assert verdict.reason is not None and "timed out" in verdict.reason
    assert len(broker.cancels) == 1


def test_unknown_decision_value_is_treated_as_block() -> None:
    # A decision payload we cannot read as an explicit "approve" must NOT be
    # allowed through — fail closed.
    broker = StubBroker()
    handler = _handler(broker, [_policy()])

    async def go() -> ToolCallVerdict | None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        handler.resolve_decision({"request_id": rid, "decision": "maybe-later"})
        return await task

    verdict = asyncio.run(go())
    assert isinstance(verdict, ToolCallVerdict)
    assert verdict.block is True
    assert len(broker.cancels) == 1


# ---------------------------------------------------------------------------
# fail-closed matching at the handler boundary
# ---------------------------------------------------------------------------


def test_condition_with_missing_field_still_gates() -> None:
    # Policy condition references a field absent from params. find_matching_policy
    # is fail-closed (missing field => match), so the call MUST be gated, not
    # waved through. We then reject it to confirm the gate held.
    broker = StubBroker()
    handler = _handler(
        broker, [_policy(condition={"field": "amount", "op": ">", "value": 100})]
    )

    async def go() -> ToolCallVerdict | None:
        task = asyncio.create_task(
            handler.on_pre_tool_use(_event("mcp__email__send", {"subject": "hi"}))
        )
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        handler.resolve_decision({"request_id": rid, "decision": "reject"})
        return await task

    verdict = asyncio.run(go())
    assert isinstance(verdict, ToolCallVerdict)
    assert verdict.block is True


def test_condition_definitively_false_allows_without_gate() -> None:
    # amount(50) is NOT > 100 -> condition false -> no policy match -> allow.
    broker = StubBroker()
    handler = _handler(
        broker, [_policy(condition={"field": "amount", "op": ">", "value": 100})]
    )
    verdict = asyncio.run(
        handler.on_pre_tool_use(_event("mcp__email__send", {"amount": 50}))
    )
    assert verdict is None
    assert broker.published == []


# ---------------------------------------------------------------------------
# concurrent multi-approval in one turn (§6)
# ---------------------------------------------------------------------------


def test_concurrent_double_approval_routes_independently() -> None:
    broker = StubBroker()
    # "*" policy gates every tool on this server.
    handler = _handler(broker, [_policy(tool="*")])

    async def go() -> tuple[Any, Any]:
        task_a = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        task_b = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__delete")))
        await _wait_for_requests(broker, 2)

        rid_a = broker.requests[0]["request_id"]
        rid_b = broker.requests[1]["request_id"]
        assert rid_a != rid_b

        # Resolve out of order: B first (approve), then A (reject). If routing
        # cross-wired, A would be approved and B rejected.
        handler.resolve_decision({"request_id": rid_b, "decision": "approve"})
        handler.resolve_decision({"request_id": rid_a, "decision": "reject"})

        return await task_a, await task_b

    verdict_a, verdict_b = asyncio.run(go())
    assert isinstance(verdict_a, ToolCallVerdict) and verdict_a.block is True  # A rejected
    assert verdict_b is None                                                   # B approved
    # Exactly one cancel — for the rejected A, not the approved B.
    assert len(broker.cancels) == 1
    rid_a = broker.requests[0]["request_id"]
    assert broker.cancels[0]["request_id"] == rid_a


# ---------------------------------------------------------------------------
# finally: Stop (CancelledError) and exception paths emit cancel
# ---------------------------------------------------------------------------


def test_stop_cancellederror_emits_cancel_and_propagates() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])

    async def go() -> None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Stop must still have emitted the cancel for the in-flight request.
        assert len(broker.cancels) == 1
        assert broker.cancels[0]["request_id"] == rid

    asyncio.run(go())


def test_exception_path_emits_cancel_and_propagates() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])

    async def go() -> None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        # Inject an unexpected error at the await point (not timeout/cancel).
        handler._pending[rid].set_exception(RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await task
        assert len(broker.cancels) == 1
        assert broker.cancels[0]["request_id"] == rid

    asyncio.run(go())


# ---------------------------------------------------------------------------
# decision routing: first-wins / idempotent
# ---------------------------------------------------------------------------


def test_decision_after_timeout_is_noop() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()], timeout_ms=20)
    verdict = asyncio.run(handler.on_pre_tool_use(_event("mcp__email__send")))
    assert isinstance(verdict, ToolCallVerdict) and verdict.block is True
    rid = broker.requests[0]["request_id"]
    # The await point is gone; a late click resolves nothing.
    assert handler.resolve_decision({"request_id": rid, "decision": "approve"}) is False


def test_resolve_decision_unknown_or_malformed_request() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])
    assert handler.resolve_decision({"request_id": "nope", "decision": "approve"}) is False
    assert handler.resolve_decision({"decision": "approve"}) is False  # no request_id
    assert handler.resolve_decision({"request_id": 123, "decision": "approve"}) is False


def test_double_decision_second_is_noop() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])

    async def go() -> ToolCallVerdict | None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        assert handler.resolve_decision({"request_id": rid, "decision": "approve"}) is True
        # Future already resolved → second decision is a no-op.
        assert handler.resolve_decision({"request_id": rid, "decision": "reject"}) is False
        return await task

    verdict = asyncio.run(go())
    assert verdict is None


# ---------------------------------------------------------------------------
# request payload contract (§3.1)
# ---------------------------------------------------------------------------


def test_approval_request_payload_contract() -> None:
    broker = StubBroker()
    handler = _handler(
        broker,
        [_policy(condition={"field": "amount", "op": ">", "value": 100})],
        timeout_ms=300_000,
        now=lambda: _FIXED_NOW,
        id_factory=lambda: "req-fixed",
    )

    async def go() -> None:
        task = asyncio.create_task(
            handler.on_pre_tool_use(_event("mcp__email__send", {"amount": 500, "to": "x"}))
        )
        await _wait_for_requests(broker, 1)
        handler.resolve_decision({"request_id": "req-fixed", "decision": "approve"})
        await task

    asyncio.run(go())
    req = broker.requests[0]
    assert req["request_id"] == "req-fixed"
    assert req["tenant_id"] == "tenant-1"
    assert req["coworker_id"] == "cow-1"
    assert req["conversation_id"] == "conv-1"
    assert req["user_id"] == "user-1"
    assert req["job_id"] == "job-1"
    assert req["policy_id"] == "11111111-1111-1111-1111-111111111111"
    assert req["mcp_server_name"] == "email"
    assert req["tool_name"] == "send"
    assert req["params"] == {"amount": 500, "to": "x"}
    assert isinstance(req["action_summary"], str) and req["action_summary"]
    assert req["requested_at"] == _FIXED_NOW.isoformat()
    expected_expiry = (_FIXED_NOW + timedelta(milliseconds=300_000)).isoformat()
    assert req["expires_at"] == expected_expiry
    # Forward-compat contract: exactly the §3.1 keys, no extras.
    assert set(req) == {
        "request_id", "tenant_id", "coworker_id", "conversation_id", "user_id",
        "job_id", "policy_id", "mcp_server_name", "tool_name", "params",
        "action_summary", "requested_at", "expires_at",
    }


def test_null_user_id_forwarded_for_fail_closed_orchestrator() -> None:
    # §3.1: a null approver is forwarded as-is so the orchestrator can fail
    # closed on it (the container does not invent an approver).
    broker = StubBroker()
    handler = _handler(broker, [_policy()], user_id=None)

    async def go() -> None:
        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        handler.resolve_decision({"request_id": rid, "decision": "approve"})
        await task

    asyncio.run(go())
    assert broker.requests[0]["user_id"] is None


# ---------------------------------------------------------------------------
# R1 (§9): the block must be cooperative — the event loop stays free while an
# approval is pending, so MCP connection keepalives / NATS decision delivery
# keep running and the connection is not starved by our wait. (The remote-side
# idle-drop risk is out of scope for an in-process test; see the S2 writeup.)
# ---------------------------------------------------------------------------


def test_block_is_cooperative_loop_not_frozen() -> None:
    broker = StubBroker()
    handler = _handler(broker, [_policy()])
    keepalive_ticks = 0

    async def go() -> ToolCallVerdict | None:
        nonlocal keepalive_ticks

        async def keepalive() -> None:
            # Simulates an MCP/NATS keepalive coroutine that must keep ticking
            # while the hook is blocked. If the await blocked the loop, this
            # would never advance.
            nonlocal keepalive_ticks
            for _ in range(5):
                await asyncio.sleep(0)
                keepalive_ticks += 1

        task = asyncio.create_task(handler.on_pre_tool_use(_event("mcp__email__send")))
        ka = asyncio.create_task(keepalive())
        await _wait_for_requests(broker, 1)
        await ka  # keepalive must complete *while the hook is still blocked*
        assert not task.done()  # still awaiting the decision
        rid = broker.requests[0]["request_id"]
        handler.resolve_decision({"request_id": rid, "decision": "approve"})
        return await task

    verdict = asyncio.run(go())
    assert verdict is None
    assert keepalive_ticks == 5  # the loop kept running throughout the block
