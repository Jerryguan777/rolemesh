"""Unit tests for the shared container-side ApprovalAwaiter.

The awaiter is the block-and-await primitive both the business-policy approval
hook and the safety->approval bridge reuse. These drive it against a stub broker
(no NATS) and a hand-driven ``resolve_decision``, asserting the wire contract
(request_id / job_id / requested_at / expires_at injected onto the published
body), the approve / reject / timeout outcomes, and the §3.3 cancel discipline.

Each test runs its own loop via ``asyncio.run`` so the suite needs no
pytest-asyncio mode.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_runner.approval.awaiter import ApprovalAwaiter, ApprovalDecision

_FIXED_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


class StubBroker:
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


def _awaiter(
    broker: StubBroker,
    *,
    timeout_ms: int = 300_000,
    **kw: Any,
) -> ApprovalAwaiter:
    return ApprovalAwaiter(
        publish=broker.publish, job_id="job-1", timeout_ms=timeout_ms, **kw
    )


async def _wait_for_requests(broker: StubBroker, n: int) -> None:
    for _ in range(10_000):
        if len(broker.requests) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {n} request(s), got {len(broker.requests)}")


def test_request_body_gets_ids_and_window_stamped() -> None:
    broker = StubBroker()
    awaiter = _awaiter(
        broker, timeout_ms=300_000, now=lambda: _FIXED_NOW, id_factory=lambda: "rid"
    )

    async def go() -> None:
        task = asyncio.create_task(
            awaiter.await_decision({"tenant_id": "t1", "tool_name": "send"})
        )
        await _wait_for_requests(broker, 1)
        awaiter.resolve_decision({"request_id": "rid", "decision": "approve"})
        await task

    asyncio.run(go())
    req = broker.requests[0]
    assert req["request_id"] == "rid"
    assert req["job_id"] == "job-1"
    assert req["tenant_id"] == "t1"
    assert req["tool_name"] == "send"
    assert req["requested_at"] == _FIXED_NOW.isoformat()
    assert req["expires_at"] == (_FIXED_NOW + timedelta(milliseconds=300_000)).isoformat()
    # Subject is namespaced to the job.
    subject = broker.published[0][0]
    assert subject == "agent.job-1.approval_request"


def test_approve_returns_approved_and_does_not_cancel() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker)

    async def go() -> ApprovalDecision:
        task = asyncio.create_task(awaiter.await_decision({"tool_name": "send"}))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        assert awaiter.resolve_decision({"request_id": rid, "decision": "approve"}) is True
        return await task

    decision = asyncio.run(go())
    assert decision.approved is True
    assert decision.timed_out is False
    assert broker.cancels == []  # clean approve does not cancel (§3.3)


def test_reject_carries_note_and_emits_cancel() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker)

    async def go() -> ApprovalDecision:
        task = asyncio.create_task(awaiter.await_decision({"tool_name": "send"}))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        awaiter.resolve_decision(
            {"request_id": rid, "decision": "reject", "note": "too risky"}
        )
        return await task

    decision = asyncio.run(go())
    assert decision.approved is False
    assert decision.timed_out is False
    assert decision.note == "too risky"
    assert len(broker.cancels) == 1


def test_unknown_decision_is_a_deny() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker)

    async def go() -> ApprovalDecision:
        task = asyncio.create_task(awaiter.await_decision({"tool_name": "send"}))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        awaiter.resolve_decision({"request_id": rid, "decision": "maybe-later"})
        return await task

    decision = asyncio.run(go())
    assert decision.approved is False
    assert decision.timed_out is False
    assert len(broker.cancels) == 1


def test_timeout_returns_timed_out_and_cancels() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker, timeout_ms=20)
    decision = asyncio.run(awaiter.await_decision({"tool_name": "send"}))
    assert decision.timed_out is True
    assert decision.approved is False
    assert len(broker.cancels) == 1


def test_concurrent_requests_route_independently() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker)

    async def go() -> tuple[ApprovalDecision, ApprovalDecision]:
        task_a = asyncio.create_task(awaiter.await_decision({"tool_name": "a"}))
        await _wait_for_requests(broker, 1)
        task_b = asyncio.create_task(awaiter.await_decision({"tool_name": "b"}))
        await _wait_for_requests(broker, 2)
        rid_a = broker.requests[0]["request_id"]
        rid_b = broker.requests[1]["request_id"]
        assert rid_a != rid_b
        awaiter.resolve_decision({"request_id": rid_b, "decision": "approve"})
        awaiter.resolve_decision({"request_id": rid_a, "decision": "reject"})
        return await task_a, await task_b

    dec_a, dec_b = asyncio.run(go())
    assert dec_a.approved is False  # A rejected
    assert dec_b.approved is True   # B approved
    assert len(broker.cancels) == 1  # only the rejected A cancels


def test_resolve_unknown_or_late_is_noop() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker, timeout_ms=20)
    asyncio.run(awaiter.await_decision({"tool_name": "send"}))
    rid = broker.requests[0]["request_id"]
    # The await point is gone; a late decision routes nothing.
    assert awaiter.resolve_decision({"request_id": rid, "decision": "approve"}) is False
    assert awaiter.resolve_decision({"decision": "approve"}) is False
    assert awaiter.resolve_decision({"request_id": 123, "decision": "approve"}) is False


def test_stop_cancellederror_emits_cancel_and_propagates() -> None:
    broker = StubBroker()
    awaiter = _awaiter(broker)

    async def go() -> None:
        task = asyncio.create_task(awaiter.await_decision({"tool_name": "send"}))
        await _wait_for_requests(broker, 1)
        rid = broker.requests[0]["request_id"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(broker.cancels) == 1
        assert broker.cancels[0]["request_id"] == rid

    asyncio.run(go())
