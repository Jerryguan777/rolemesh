"""ApprovalNotifier delivery layer (docs/21-hitl-approval-plan.md §10 S4).

Drives the card lifecycle — target resolution, Telegram vs web delivery, and
the deterministic terminal edit — against fakes at the *outermost* boundaries
(the channel send/edit/publish callables). No broker, no Postgres, no live
Telegram: the resolution + cache logic is what's under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from rolemesh.core.types import ChannelBinding, Conversation
from rolemesh.db.approval import ApprovalRequest
from rolemesh.orchestration.approval_notify import ApprovalNotifier


def _req(
    *,
    request_id: str = "req1",
    tenant_id: str = "t1",
    coworker_id: str = "cw1",
    conversation_id: str | None = "conv1",
    action_summary: str | None = "stripe.charge(amount)",
) -> ApprovalRequest:
    now = datetime.now(tz=UTC)
    return ApprovalRequest(
        id=request_id,
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        conversation_id=conversation_id,
        policy_id="pol1",
        user_id="user1",
        job_id="job1",
        mcp_server_name="stripe",
        action={"tool_name": "charge", "params": {"amount": 500}},
        action_summary=action_summary,
        status="pending",
        decided_by=None,
        note=None,
        requested_at=now,
        expires_at=now + timedelta(minutes=5),
        decided_at=None,
    )


def _conv(
    *,
    conv_id: str = "conv1",
    binding_id: str = "bind1",
    chat_id: str = "chat1",
    last_agent_invocation: str | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> Conversation:
    return Conversation(
        id=conv_id,
        tenant_id="t1",
        coworker_id="cw1",
        channel_binding_id=binding_id,
        channel_chat_id=chat_id,
        last_agent_invocation=last_agent_invocation,
        created_at=created_at,
    )


def _binding(channel_type: str, binding_id: str = "bind1") -> ChannelBinding:
    return ChannelBinding(
        id=binding_id, coworker_id="cw1", tenant_id="t1", channel_type=channel_type
    )


class _Harness:
    """Records every outermost-boundary call the notifier makes."""

    def __init__(
        self,
        *,
        conversations: dict[str, Conversation] | None = None,
        bindings: dict[str, ChannelBinding] | None = None,
        coworker_convs: list[Conversation] | None = None,
        telegram_message_id: int | None = 42,
    ) -> None:
        self.conversations = conversations or {}
        self.bindings = bindings or {}
        self.coworker_convs = coworker_convs or []
        self._tg_message_id = telegram_message_id
        self.tg_sends: list[tuple[str, str, str, str]] = []
        self.tg_edits: list[tuple[str, str, int, str]] = []
        self.web_events: list[tuple[str, str, dict[str, Any]]] = []

    async def get_conversation(self, conv_id: str) -> Conversation | None:
        return self.conversations.get(conv_id)

    async def get_binding(self, binding_id: str) -> ChannelBinding | None:
        return self.bindings.get(binding_id)

    async def list_convs(self, coworker_id: str, tenant_id: str) -> list[Conversation]:
        return list(self.coworker_convs)

    async def send_tg(
        self, binding_id: str, chat_id: str, request_id: str, summary: str
    ) -> int | None:
        self.tg_sends.append((binding_id, chat_id, request_id, summary))
        return self._tg_message_id

    async def edit_tg(
        self, binding_id: str, chat_id: str, message_id: int, text: str
    ) -> None:
        self.tg_edits.append((binding_id, chat_id, message_id, text))

    async def publish_web(
        self, binding_id: str, chat_id: str, payload: dict[str, Any]
    ) -> None:
        self.web_events.append((binding_id, chat_id, payload))

    def notifier(self) -> ApprovalNotifier:
        return ApprovalNotifier(
            get_conversation=self.get_conversation,
            get_binding=self.get_binding,
            list_conversations_for_coworker=self.list_convs,
            send_telegram_card=self.send_tg,
            edit_telegram_card=self.edit_tg,
            publish_web_event=self.publish_web,
        )


# ---------------------------------------------------------------------------
# Telegram delivery + hard edit
# ---------------------------------------------------------------------------


async def test_telegram_card_delivered_then_edited_on_reject() -> None:
    h = _Harness(
        conversations={"conv1": _conv()},
        bindings={"bind1": _binding("telegram")},
    )
    n = h.notifier()
    req = _req()

    await n.notify_status(req)
    assert h.tg_sends == [("bind1", "chat1", "req1", "stripe.charge(amount)")]
    # The card location must be cached so the IDOR funnel can authorise the tap.
    ref = n.card_ref("req1")
    assert ref is not None
    assert (ref.tenant_id, ref.conversation_id, ref.chat_id) == ("t1", "conv1", "chat1")
    assert ref.telegram_message_id == 42

    await n.notify_hard(req, "rejected")
    assert h.tg_edits == [("bind1", "chat1", 42, "❌ Rejected")]
    # Terminal — the cache entry is gone, so a racing second resolve no-ops.
    assert n.card_ref("req1") is None


async def test_telegram_card_edited_on_expiry() -> None:
    h = _Harness(
        conversations={"conv1": _conv()}, bindings={"bind1": _binding("telegram")}
    )
    n = h.notifier()
    req = _req()
    await n.notify_status(req)
    await n.notify_hard(req, "expired")
    assert h.tg_edits[0][3].startswith("⏰")


async def test_approve_outcome_edits_card() -> None:
    h = _Harness(
        conversations={"conv1": _conv()}, bindings={"bind1": _binding("telegram")}
    )
    n = h.notifier()
    await n.notify_status(_req())
    await n.mark_outcome("req1", "approved")
    assert h.tg_edits == [("bind1", "chat1", 42, "✅ Approved")]


async def test_failed_telegram_send_skips_edit_but_does_not_crash() -> None:
    # message_id None (send failed) ⇒ no edit attempted, no exception.
    h = _Harness(
        conversations={"conv1": _conv()},
        bindings={"bind1": _binding("telegram")},
        telegram_message_id=None,
    )
    n = h.notifier()
    req = _req()
    await n.notify_status(req)
    await n.notify_hard(req, "rejected")
    assert h.tg_edits == []


# ---------------------------------------------------------------------------
# Web delivery + resolution events
# ---------------------------------------------------------------------------


async def test_web_card_emits_requested_then_resolved() -> None:
    h = _Harness(
        conversations={"conv1": _conv()}, bindings={"bind1": _binding("web")}
    )
    n = h.notifier()
    req = _req()
    await n.notify_status(req)
    assert len(h.web_events) == 1
    binding_id, chat_id, payload = h.web_events[0]
    assert (binding_id, chat_id) == ("bind1", "chat1")
    assert payload["type"] == "approval.requested"
    assert payload["request_id"] == "req1"
    assert payload["action_summary"] == "stripe.charge(amount)"

    await n.notify_hard(req, "rejected")
    assert h.web_events[1][2] == {
        "type": "approval.resolved",
        "request_id": "req1",
        "outcome": "rejected",
    }
    # No Telegram traffic for a web conversation.
    assert h.tg_sends == [] and h.tg_edits == []


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


async def test_missing_conversation_sends_nothing() -> None:
    h = _Harness(conversations={}, bindings={"bind1": _binding("telegram")})
    n = h.notifier()
    await n.notify_status(_req(conversation_id="ghost"))
    assert h.tg_sends == [] and h.web_events == []
    assert n.card_ref("req1") is None


async def test_scheduled_task_falls_back_to_most_recent_conversation() -> None:
    # No conversation on the request (scheduled task). The notifier must pick
    # the coworker's most recently active conversation, not just any.
    stale = _conv(
        conv_id="old", binding_id="bind1", chat_id="chatOld",
        last_agent_invocation="2026-01-01T00:00:00+00:00",
    )
    recent = _conv(
        conv_id="new", binding_id="bind1", chat_id="chatNew",
        last_agent_invocation="2026-05-30T00:00:00+00:00",
    )
    h = _Harness(
        bindings={"bind1": _binding("telegram")},
        coworker_convs=[stale, recent],
    )
    n = h.notifier()
    await n.notify_status(_req(conversation_id=None))
    assert h.tg_sends == [("bind1", "chatNew", "req1", "stripe.charge(amount)")]


async def test_scheduled_task_with_no_conversations_sends_nothing() -> None:
    h = _Harness(bindings={"bind1": _binding("telegram")}, coworker_convs=[])
    n = h.notifier()
    await n.notify_status(_req(conversation_id=None))
    assert h.tg_sends == [] and h.web_events == []


async def test_resolve_when_binding_row_missing_sends_nothing() -> None:
    h = _Harness(conversations={"conv1": _conv()}, bindings={})
    n = h.notifier()
    await n.notify_status(_req())
    assert h.tg_sends == [] and h.web_events == []


# ---------------------------------------------------------------------------
# Restart degradation: a resolve with no cached card (orchestrator restarted)
# must be a clean no-op, never an exception.
# ---------------------------------------------------------------------------


async def test_mark_outcome_without_card_is_noop() -> None:
    h = _Harness()
    n = h.notifier()
    await n.mark_outcome("never-delivered", "expired")
    assert h.tg_edits == [] and h.web_events == []
