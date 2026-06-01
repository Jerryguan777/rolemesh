"""ApprovalCoordinator state machine (docs/21-hitl-approval-plan.md §8).

Exercises the orchestrator-side suspend/resume, expiry sweep, decision-race
idempotency, and restart recovery against a *real* GroupQueue plus an in-memory
store that faithfully mirrors ``resolve_approval_request``'s first-wins
``WHERE status='pending'`` guard. No broker, no Postgres — so the race-prone
timer logic is the thing under test, not the plumbing.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from rolemesh.container.scheduler import GroupQueue
from rolemesh.db.approval import ApprovalRequest
from rolemesh.orchestration.approval_coordinator import (
    ApprovalCoordinator,
    ApprovalPersistence,
    approval_queue_key,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class _FakeNats:
    async def request(self, _subject: str, _payload: bytes, timeout: float | None = None) -> Any:
        return SimpleNamespace(data=b"ack")


class _FakeJS:
    async def publish(self, _subject: str, _payload: bytes) -> None:
        return None


class _FakeTransport:
    def __init__(self) -> None:
        self.nc = _FakeNats()
        self.js = _FakeJS()


class _FakeStore:
    """In-memory approval store mirroring the real first-wins idempotency."""

    def __init__(self) -> None:
        self.rows: dict[str, ApprovalRequest] = {}

    async def create_request(
        self,
        *,
        tenant_id: str,
        coworker_id: str,
        job_id: str,
        mcp_server_name: str,
        action: dict[str, Any],
        expires_at: datetime,
        conversation_id: str | None = None,
        policy_id: str | None = None,
        user_id: str | None = None,
        action_summary: str | None = None,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        rid = request_id or str(uuid.uuid4())
        row = ApprovalRequest(
            id=rid,
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=policy_id,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=mcp_server_name,
            action=action,
            action_summary=action_summary,
            status="pending",
            decided_by=None,
            note=None,
            requested_at=_now(),
            expires_at=expires_at,
            decided_at=None,
        )
        self.rows[rid] = row
        return row

    async def resolve_request(
        self,
        request_id: str,
        *,
        tenant_id: str,
        status: str,
        decided_by: str | None = None,
        note: str | None = None,
    ) -> ApprovalRequest | None:
        row = self.rows.get(request_id)
        # First-wins + tenant scope, exactly like the SQL WHERE clause.
        if row is None or row.tenant_id != tenant_id or row.status != "pending":
            return None
        new = replace(
            row, status=status, decided_by=decided_by, note=note, decided_at=_now()
        )
        self.rows[request_id] = new
        return new

    async def list_pending_all(self) -> list[ApprovalRequest]:
        return [r for r in self.rows.values() if r.status == "pending"]


def _make(idle_ms: int = 10_000) -> tuple[ApprovalCoordinator, GroupQueue, _FakeStore, list[Any]]:
    q = GroupQueue(transport=_FakeTransport(), idle_timeout_ms=idle_ms)
    store = _FakeStore()
    decisions: list[Any] = []

    async def _publish(job_id: str, payload: dict[str, Any]) -> None:
        decisions.append((job_id, payload))

    coord = ApprovalCoordinator(
        queue=q,
        persistence=ApprovalPersistence(
            store.create_request, store.resolve_request, store.list_pending_all
        ),
        resolve_tenant=lambda _cw: "t1",
        publish_decision=_publish,
        now=_now,
    )
    return coord, q, store, decisions


def _payload(
    *,
    request_id: str = "req1",
    job_id: str = "job1",
    coworker_id: str = "cw1",
    conversation_id: str | None = "conv1",
    user_id: str | None = "user1",
    expires_in_ms: int = 300_000,
) -> dict[str, Any]:
    base = _now()
    return {
        "request_id": request_id,
        "tenant_id": "t1",
        "coworker_id": coworker_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "job_id": job_id,
        "policy_id": "pol1",
        "mcp_server_name": "stripe",
        "tool_name": "charge",
        "params": {"amount": 500},
        "action_summary": "stripe.charge",
        "requested_at": base.isoformat(),
        "expires_at": (base + timedelta(milliseconds=expires_in_ms)).isoformat(),
    }


# ---------------------------------------------------------------------------
# queue key
# ---------------------------------------------------------------------------


def test_queue_key_prefers_conversation_then_coworker() -> None:
    assert approval_queue_key("conv1", "cw1") == "conv1"
    assert approval_queue_key(None, "cw1") == "cw1"
    assert approval_queue_key("", "cw1") == "cw1"


# ---------------------------------------------------------------------------
# suspend on request
# ---------------------------------------------------------------------------


async def test_request_suspends_and_persists() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload())

    assert q.is_awaiting_approval("conv1") is True
    assert store.rows["req1"].status == "pending"
    # The persisted row id MUST equal the container's request_id (decision relay
    # routes by it).
    assert "req1" in store.rows
    assert q._get_group("conv1").idle_handle is None  # suspended, not armed
    assert decisions == []


async def test_request_keyed_on_coworker_when_no_conversation() -> None:
    coord, q, _store, _ = _make()
    await coord.on_approval_request(_payload(conversation_id=None))
    assert q.is_awaiting_approval("cw1") is True


# ---------------------------------------------------------------------------
# decide → relay + resume
# ---------------------------------------------------------------------------


async def test_approve_relays_decision_and_resumes() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload())

    assert await coord.decide("req1", decision="approve", decided_by="user1") is True
    assert decisions == [
        ("job1", {"request_id": "req1", "decision": "approve", "decided_by": "user1", "note": None})
    ]
    assert store.rows["req1"].status == "approved"
    assert q.is_awaiting_approval("conv1") is False
    assert q._get_group("conv1").idle_handle is not None  # re-armed


async def test_reject_relays_decision_and_resumes() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload())

    assert await coord.decide("req1", decision="reject", decided_by="user1", note="no") is True
    assert decisions[0][1]["decision"] == "reject"
    assert store.rows["req1"].status == "rejected"
    assert q.is_awaiting_approval("conv1") is False


async def test_null_approver_auto_rejects() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload(user_id=None))

    assert store.rows["req1"].status == "rejected"
    assert decisions and decisions[0][1]["decision"] == "reject"
    assert q.is_awaiting_approval("conv1") is False


async def test_decide_unknown_request_is_noop() -> None:
    coord, _q, _store, decisions = _make()
    assert await coord.decide("nope", decision="approve", decided_by="u") is False
    assert decisions == []


# ---------------------------------------------------------------------------
# IDOR guard (S4): a guessed request_id must not let one tenant / conversation
# decide another's approval. The guard runs BEFORE any DB write or relay.
# ---------------------------------------------------------------------------


async def test_decide_refuses_tenant_mismatch() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload())  # tenant t1, conv1

    # An attacker in tenant "evil" guesses req1's UUID and tries to approve it.
    won = await coord.decide(
        "req1", decision="approve", decided_by="attacker",
        expected_tenant_id="evil",
    )
    assert won is False
    assert decisions == []                      # no tool relay
    assert store.rows["req1"].status == "pending"  # row untouched
    assert q.is_awaiting_approval("conv1") is True  # still suspended


async def test_decide_refuses_conversation_mismatch() -> None:
    coord, _q, store, decisions = _make()
    await coord.on_approval_request(_payload())  # conv1

    # Same tenant, but a user who only holds a ticket for conv2.
    won = await coord.decide(
        "req1", decision="approve", decided_by="user1",
        expected_tenant_id="t1", expected_conversation_id="conv2",
    )
    assert won is False
    assert decisions == []
    assert store.rows["req1"].status == "pending"


async def test_decide_allows_matching_tenant_and_conversation() -> None:
    coord, _q, store, decisions = _make()
    await coord.on_approval_request(_payload())

    won = await coord.decide(
        "req1", decision="approve", decided_by="user1",
        expected_tenant_id="t1", expected_conversation_id="conv1",
    )
    assert won is True
    assert store.rows["req1"].status == "approved"
    assert decisions and decisions[0][1]["decision"] == "approve"


# ---------------------------------------------------------------------------
# notify_hard wiring: the hard channel fires deterministically on reject /
# expiry (never on approve — that closes the loop via the decision funnel).
# ---------------------------------------------------------------------------


def _make_with_notify(
    idle_ms: int = 10_000,
) -> tuple[ApprovalCoordinator, GroupQueue, _FakeStore, list[Any], list[Any]]:
    q = GroupQueue(transport=_FakeTransport(), idle_timeout_ms=idle_ms)
    store = _FakeStore()
    decisions: list[Any] = []
    hard: list[Any] = []

    async def _publish(job_id: str, payload: dict[str, Any]) -> None:
        decisions.append((job_id, payload))

    async def _notify_hard(req: ApprovalRequest, kind: str) -> None:
        hard.append((req.id, kind))

    coord = ApprovalCoordinator(
        queue=q,
        persistence=ApprovalPersistence(
            store.create_request, store.resolve_request, store.list_pending_all
        ),
        resolve_tenant=lambda _cw: "t1",
        publish_decision=_publish,
        now=_now,
        notify_hard=_notify_hard,
    )
    return coord, q, store, decisions, hard


async def test_notify_hard_fires_on_reject_only_with_kind_rejected() -> None:
    coord, _q, _store, _decisions, hard = _make_with_notify()
    await coord.on_approval_request(_payload())
    await coord.decide("req1", decision="reject", decided_by="user1")
    assert hard == [("req1", "rejected")]


async def test_notify_hard_does_not_fire_on_approve() -> None:
    coord, _q, _store, _decisions, hard = _make_with_notify()
    await coord.on_approval_request(_payload())
    await coord.decide("req1", decision="approve", decided_by="user1")
    assert hard == []


async def test_notify_hard_fires_on_expiry_with_kind_expired() -> None:
    coord, _q, _store, _decisions, hard = _make_with_notify()
    await coord.on_approval_request(_payload(expires_in_ms=30))
    await asyncio.sleep(0.08)
    assert hard == [("req1", "expired")]


# ---------------------------------------------------------------------------
# the decision race (the headline correctness property)
# ---------------------------------------------------------------------------


async def test_cancel_after_decision_is_idempotent() -> None:
    coord, q, store, _decisions = _make()
    await coord.on_approval_request(_payload())

    assert await coord.decide("req1", decision="reject", decided_by="user1") is True
    state = q._get_group("conv1")
    handle_after_decision = state.idle_handle
    assert handle_after_decision is not None

    # S2 also fires approval_cancel on reject — must be a clean no-op.
    await coord.on_approval_cancel({"request_id": "req1"})
    assert store.rows["req1"].status == "rejected"  # cancel did not override
    assert state.idle_handle is handle_after_decision  # no double re-arm
    assert q.is_awaiting_approval("conv1") is False


async def test_cancel_only_resumes_without_relaying_decision() -> None:
    # Container timeout / Stop / exception path: only a cancel arrives.
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload())

    await coord.on_approval_cancel({"request_id": "req1"})
    assert store.rows["req1"].status == "cancelled"
    assert decisions == []
    assert q.is_awaiting_approval("conv1") is False


async def test_decide_after_expiry_does_not_relay_approve() -> None:
    # A late approve click must never run a tool whose request already expired.
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload())
    # Expiry wins the row first.
    assert await store.resolve_request("req1", tenant_id="t1", status="expired") is not None

    assert await coord.decide("req1", decision="approve", decided_by="user1") is False
    assert decisions == []
    assert store.rows["req1"].status == "expired"
    assert q.is_awaiting_approval("conv1") is False


# ---------------------------------------------------------------------------
# concurrent multi-approval in one turn
# ---------------------------------------------------------------------------


async def test_concurrent_double_approval_independent_and_last_rearms() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload(request_id="req1"))
    await coord.on_approval_request(_payload(request_id="req2"))

    state = q._get_group("conv1")
    assert state.awaiting_approval == {"req1", "req2"}

    assert await coord.decide("req1", decision="approve", decided_by="user1") is True
    assert state.idle_handle is None  # req2 still pending
    assert await coord.decide("req2", decision="approve", decided_by="user1") is True
    assert state.idle_handle is not None  # last one re-arms

    assert {d[1]["request_id"] for d in decisions} == {"req1", "req2"}
    assert store.rows["req1"].status == "approved"
    assert store.rows["req2"].status == "approved"


# ---------------------------------------------------------------------------
# expiry watcher
# ---------------------------------------------------------------------------


async def test_expiry_marks_expired_and_resumes() -> None:
    coord, q, store, decisions = _make()
    await coord.on_approval_request(_payload(expires_in_ms=30))

    await asyncio.sleep(0.08)
    assert store.rows["req1"].status == "expired"
    assert decisions == []  # no decision relayed on expiry
    assert q.is_awaiting_approval("conv1") is False


async def test_decision_before_expiry_cancels_the_watcher() -> None:
    coord, _q, store, _decisions = _make()
    await coord.on_approval_request(_payload(expires_in_ms=40))
    assert await coord.decide("req1", decision="approve", decided_by="user1") is True

    await asyncio.sleep(0.08)  # the original expiry instant passes
    # Expiry must NOT have flipped the already-approved row.
    assert store.rows["req1"].status == "approved"


# ---------------------------------------------------------------------------
# restart recovery (R2)
# ---------------------------------------------------------------------------


async def test_restart_recovery_readopts_live_not_reaped() -> None:
    coord, q, store, decisions = _make()
    # A previous orchestrator left this pending; _groups is empty (fresh queue).
    future = _now() + timedelta(minutes=5)
    await store.create_request(
        request_id="req1", tenant_id="t1", coworker_id="cw1", job_id="job1",
        mcp_server_name="stripe", action={"tool_name": "charge", "params": {}},
        expires_at=future, conversation_id="conv1", user_id="user1",
    )

    await coord.recover_pending()

    state = q._get_group("conv1")
    assert state.active is True
    assert state.adopted is True
    assert state.job_id == "job1"
    assert q.is_awaiting_approval("conv1") is True  # re-suspended ⇒ not reaped
    assert state.idle_handle is None  # not armed ⇒ not reaped

    # And it is resumable: a decision relays to the (re-adopted) container.
    assert await coord.decide("req1", decision="approve", decided_by="user1") is True
    assert decisions == [
        ("job1", {"request_id": "req1", "decision": "approve", "decided_by": "user1", "note": None})
    ]
    assert state.idle_handle is not None  # adopted reaper armed after resume


async def test_restart_recovery_expires_past_deadline() -> None:
    coord, q, store, _decisions = _make()
    past = _now() - timedelta(minutes=1)
    await store.create_request(
        request_id="req1", tenant_id="t1", coworker_id="cw1", job_id="job1",
        mcp_server_name="stripe", action={"tool_name": "charge", "params": {}},
        expires_at=past, conversation_id="conv1", user_id="user1",
    )

    await coord.recover_pending()

    assert store.rows["req1"].status == "expired"
    # No adoption for an already-expired request: the conversation is untouched.
    assert q.is_awaiting_approval("conv1") is False
    assert q._groups.get("conv1") is None


async def test_restart_recovery_is_idempotent_across_double_run() -> None:
    coord, q, store, _decisions = _make()
    future = _now() + timedelta(minutes=5)
    await store.create_request(
        request_id="req1", tenant_id="t1", coworker_id="cw1", job_id="job1",
        mcp_server_name="stripe", action={"tool_name": "charge", "params": {}},
        expires_at=future, conversation_id="conv1", user_id="user1",
    )
    await coord.recover_pending()
    await coord.recover_pending()  # a retry must not double-suspend

    assert q._get_group("conv1").awaiting_approval == {"req1"}
