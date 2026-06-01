"""End-to-end HITL decision funnel (docs/21-hitl-approval-plan.md §10 S4).

Wires a *real* ApprovalCoordinator + ApprovalNotifier and drives the main.py
Telegram / web decision funnels, mocking only the outermost boundaries (the
channel send/edit/publish callables and the identity-resolution DB helpers).

The chain under test, per the unattended-run requirement:

    card delivery → decision intake (tap / WS frame) → coordinator.decide →
    NATS relay to the container (publish_decision) → hard-channel card edit

The container-side unblock that consumes ``agent.{job_id}.approval_decision``
is covered by the S2 hook tests; here the relay payload is the seam asserted.
The headline correctness property is the IDOR guard: an unlinked sender, a tap
from a foreign chat, or a cross-tenant web frame never reaches ``decide``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import rolemesh.db as db_module
import rolemesh.db.channel_identity as identity_module
import rolemesh.main as main_module
from rolemesh.container.scheduler import GroupQueue
from rolemesh.core.types import ChannelBinding, Conversation
from rolemesh.db.approval import ApprovalRequest
from rolemesh.orchestration.approval_coordinator import (
    ApprovalCoordinator,
    ApprovalPersistence,
)
from rolemesh.orchestration.approval_notify import ApprovalNotifier

if TYPE_CHECKING:
    import pytest


def _now() -> datetime:
    return datetime.now(tz=UTC)


class _FakeJS:
    async def publish(self, _s: str, _p: bytes) -> None:
        return None


class _FakeTransport:
    def __init__(self) -> None:
        self.nc = SimpleNamespace()
        self.js = _FakeJS()


class _Store:
    def __init__(self) -> None:
        self.rows: dict[str, ApprovalRequest] = {}

    async def create_request(self, *, request_id: str | None = None, **kw: Any) -> ApprovalRequest:
        rid = request_id or str(uuid.uuid4())
        row = ApprovalRequest(
            id=rid, status="pending", decided_by=None, note=None,
            requested_at=_now(), decided_at=None,
            policy_id=kw.get("policy_id"), user_id=kw.get("user_id"),
            action_summary=kw.get("action_summary"),
            rationale=kw.get("rationale"),
            conversation_id=kw.get("conversation_id"),
            tenant_id=kw["tenant_id"], coworker_id=kw["coworker_id"],
            job_id=kw["job_id"], mcp_server_name=kw["mcp_server_name"],
            action=kw["action"], expires_at=kw["expires_at"],
        )
        self.rows[rid] = row
        return row

    async def resolve_request(
        self, request_id: str, *, tenant_id: str, status: str,
        decided_by: str | None = None, note: str | None = None,
    ) -> ApprovalRequest | None:
        row = self.rows.get(request_id)
        if row is None or row.tenant_id != tenant_id or row.status != "pending":
            return None
        new = replace(row, status=status, decided_by=decided_by, note=note, decided_at=_now())
        self.rows[request_id] = new
        return new

    async def list_pending_all(self) -> list[ApprovalRequest]:
        return [r for r in self.rows.values() if r.status == "pending"]


class _Channels:
    """Outermost channel boundary captures (Telegram + web)."""

    def __init__(self, channel_type: str) -> None:
        self.channel_type = channel_type
        self.tg_sends: list[Any] = []
        self.tg_edits: list[Any] = []
        self.web_events: list[Any] = []

    async def get_conversation(self, _cid: str) -> Conversation | None:
        return Conversation(
            id="conv1", tenant_id="t1", coworker_id="cw1",
            channel_binding_id="bind1", channel_chat_id="chat1",
        )

    async def get_binding(self, _bid: str) -> ChannelBinding | None:
        return ChannelBinding(
            id="bind1", coworker_id="cw1", tenant_id="t1", channel_type=self.channel_type
        )

    async def list_convs(self, _cw: str, _t: str) -> list[Conversation]:
        return []

    async def send_tg(self, b: str, c: str, r: str, s: str) -> int | None:
        self.tg_sends.append((b, c, r, s))
        return 555

    async def edit_tg(self, b: str, c: str, m: int, t: str) -> None:
        self.tg_edits.append((b, c, m, t))

    async def publish_web(self, b: str, c: str, payload: dict[str, Any]) -> None:
        self.web_events.append((b, c, payload))


def _build(channel_type: str) -> tuple[ApprovalCoordinator, ApprovalNotifier, _Store, list[Any], _Channels]:
    q = GroupQueue(transport=_FakeTransport(), idle_timeout_ms=10_000)
    store = _Store()
    relays: list[Any] = []
    ch = _Channels(channel_type)

    async def _publish_decision(job_id: str, payload: dict[str, Any]) -> None:
        relays.append((job_id, payload))

    notifier = ApprovalNotifier(
        get_conversation=ch.get_conversation,
        get_binding=ch.get_binding,
        list_conversations_for_coworker=ch.list_convs,
        send_telegram_card=ch.send_tg,
        edit_telegram_card=ch.edit_tg,
        publish_web_event=ch.publish_web,
    )
    coord = ApprovalCoordinator(
        queue=q,
        persistence=ApprovalPersistence(
            store.create_request, store.resolve_request, store.list_pending_all
        ),
        resolve_tenant=lambda _cw: "t1",
        publish_decision=_publish_decision,
        now=_now,
        notify_status=notifier.notify_status,
        notify_hard=notifier.notify_hard,
    )
    return coord, notifier, store, relays, ch


def _request_payload() -> dict[str, Any]:
    base = _now()
    return {
        "request_id": "req1", "tenant_id": "t1", "coworker_id": "cw1",
        "conversation_id": "conv1", "user_id": "user1", "job_id": "job1",
        "policy_id": "pol1", "mcp_server_name": "stripe", "tool_name": "charge",
        "params": {"amount": 500}, "action_summary": "stripe.charge(amount)",
        "requested_at": base.isoformat(),
        "expires_at": (base + timedelta(minutes=5)).isoformat(),
    }


def _install(monkeypatch: pytest.MonkeyPatch, coord: ApprovalCoordinator, notifier: ApprovalNotifier) -> None:
    monkeypatch.setattr(main_module, "_approval_coordinator", coord)
    monkeypatch.setattr(main_module, "_approval_notifier", notifier)


# ===========================================================================
# Telegram channel
# ===========================================================================


async def test_telegram_approve_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, store, relays, ch = _build("telegram")
    _install(monkeypatch, coord, notifier)
    # The tapping Telegram account resolves to the request's approver.
    monkeypatch.setattr(
        identity_module, "resolve_user_from_channel_sender",
        lambda tenant, platform, cid: _ret("user1"),
    )

    # 1. Container blocks → orchestrator suspends + delivers the card.
    await coord.on_approval_request(_request_payload())
    assert ch.tg_sends == [("bind1", "chat1", "req1", "stripe.charge(amount)")]

    # 2. User taps ✅. main funnel resolves identity + decides.
    toast = await main_module._telegram_approval_decision("req1", "approve", "7", "chat1")
    assert toast == "Approved ✅"

    # 3. The decision relays to the container (S2 hook unblocks → tool runs).
    assert relays == [("job1", {
        "request_id": "req1", "decision": "approve", "decided_by": "user1", "note": None,
    })]
    # 4. Hard channel: the card is edited to the approved terminal state.
    assert ch.tg_edits == [("bind1", "chat1", 555, "✅ Approved")]
    assert store.rows["req1"].status == "approved"
    assert store.rows["req1"].decided_by == "user1"


async def test_telegram_reject_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, store, relays, ch = _build("telegram")
    _install(monkeypatch, coord, notifier)
    monkeypatch.setattr(
        identity_module, "resolve_user_from_channel_sender",
        lambda *a: _ret("user1"),
    )
    await coord.on_approval_request(_request_payload())

    toast = await main_module._telegram_approval_decision("req1", "reject", "7", "chat1")
    assert toast == "Rejected ❌"
    # Reject relays to the container (hook returns block reason to the agent).
    assert relays[0][1]["decision"] == "reject"
    # Hard channel edit via the coordinator's notify_hard.
    assert ch.tg_edits == [("bind1", "chat1", 555, "❌ Rejected")]
    assert store.rows["req1"].status == "rejected"


async def test_telegram_unlinked_sender_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, store, relays, ch = _build("telegram")
    _install(monkeypatch, coord, notifier)
    # No RoleMesh identity for this Telegram account.
    monkeypatch.setattr(
        identity_module, "resolve_user_from_channel_sender", lambda *a: _ret(None)
    )
    await coord.on_approval_request(_request_payload())

    toast = await main_module._telegram_approval_decision("req1", "approve", "999", "chat1")
    assert "not linked" in (toast or "").lower()
    assert relays == []                          # never reached the container
    assert store.rows["req1"].status == "pending"  # still awaiting a real decision
    assert ch.tg_edits == []


async def test_telegram_tap_from_foreign_chat_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, store, relays, _ch = _build("telegram")
    _install(monkeypatch, coord, notifier)
    called: list[Any] = []

    def _resolver(*a: Any) -> Any:
        called.append(a)
        return _ret("user1")

    monkeypatch.setattr(identity_module, "resolve_user_from_channel_sender", _resolver)
    await coord.on_approval_request(_request_payload())

    # The card lives in chat1; a tap whose message is in chat-evil is refused
    # before identity resolution even runs.
    toast = await main_module._telegram_approval_decision("req1", "approve", "7", "chat-evil")
    assert "not authorized" in (toast or "").lower()
    assert called == []
    assert relays == []
    assert store.rows["req1"].status == "pending"


async def test_telegram_unknown_request_is_no_longer_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, _store, relays, _ch = _build("telegram")
    _install(monkeypatch, coord, notifier)
    toast = await main_module._telegram_approval_decision("ghost", "approve", "7", "chat1")
    assert "no longer pending" in (toast or "").lower()
    assert relays == []


# ===========================================================================
# Web channel
# ===========================================================================


async def test_web_approve_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, store, relays, ch = _build("web")
    _install(monkeypatch, coord, notifier)
    monkeypatch.setattr(
        db_module, "get_channel_binding_by_id_admin",
        lambda _bid: _ret(ChannelBinding(id="bind1", coworker_id="cw1", tenant_id="t1", channel_type="web")),
    )
    monkeypatch.setattr(
        db_module, "get_conversation_by_binding_and_chat",
        lambda _b, _c, *, tenant_id: _ret(SimpleNamespace(id="conv1", user_id="user1")),
    )

    await coord.on_approval_request(_request_payload())
    assert ch.web_events[0][2]["type"] == "approval.requested"

    body = {"request_id": "req1", "decision": "approve", "decided_by": "ticket-user"}
    await main_module._web_approval_decision("bind1", "chat1", body)

    assert relays == [("job1", {
        "request_id": "req1", "decision": "approve", "decided_by": "ticket-user", "note": None,
    })]
    # Hard channel: a resolved event for the browser.
    resolved = ch.web_events[1][2]
    assert resolved == {"type": "approval.resolved", "request_id": "req1", "outcome": "approved"}
    assert store.rows["req1"].status == "approved"


async def test_web_decision_cross_tenant_request_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pending request belongs to tenant t1; the WS frame arrives on a
    # binding whose tenant is "evil". The coordinator guard must refuse.
    coord, notifier, store, relays, _ch = _build("web")
    _install(monkeypatch, coord, notifier)
    monkeypatch.setattr(
        db_module, "get_channel_binding_by_id_admin",
        lambda _bid: _ret(ChannelBinding(id="bindX", coworker_id="cwX", tenant_id="evil", channel_type="web")),
    )
    monkeypatch.setattr(
        db_module, "get_conversation_by_binding_and_chat",
        lambda _b, _c, *, tenant_id: _ret(SimpleNamespace(id="convX", user_id="evil-user")),
    )
    await coord.on_approval_request(_request_payload())

    body = {"request_id": "req1", "decision": "approve", "decided_by": "evil-user"}
    await main_module._web_approval_decision("bindX", "chat1", body)
    assert relays == []
    assert store.rows["req1"].status == "pending"


async def test_web_decision_unknown_binding_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, notifier, _store, relays, _ch = _build("web")
    _install(monkeypatch, coord, notifier)
    monkeypatch.setattr(db_module, "get_channel_binding_by_id_admin", lambda _bid: _ret(None))
    body = {"request_id": "req1", "decision": "approve"}
    await main_module._web_approval_decision("bind1", "chat1", body)
    assert relays == []


# ---------------------------------------------------------------------------


async def _ret_coro(value: Any) -> Any:
    return value


def _ret(value: Any) -> Any:
    """Wrap a value in an awaitable so a lambda can stand in for an async DB call."""
    return _ret_coro(value)
