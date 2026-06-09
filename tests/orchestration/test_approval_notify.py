"""ApprovalNotifier delivery layer (docs/12-hitl-approval-architecture.md §10 S4).

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
from rolemesh.orchestration.approval_notify import (
    ApprovalNotifier,
    pending_card_text,
)


def _req(
    *,
    request_id: str = "req1",
    tenant_id: str = "t1",
    coworker_id: str = "cw1",
    conversation_id: str | None = "conv1",
    action_summary: str | None = "stripe.charge(amount)",
    rationale: str | None = None,
    action: dict[str, Any] | None = None,
    mcp_server_name: str = "stripe",
    triggered_by: dict[str, Any] | None = None,
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
        mcp_server_name=mcp_server_name,
        action=action if action is not None else {"tool_name": "charge", "params": {"amount": 500}},
        action_summary=action_summary,
        rationale=rationale,
        triggered_by=triggered_by,
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
    assert len(h.tg_sends) == 1
    assert h.tg_sends[0][:3] == ("bind1", "chat1", "req1")
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


async def test_web_requested_payload_carries_decision_fields() -> None:
    # §1.1: the web card must be informative from the push alone. The notifier
    # projects the row's {tool_name, params} snapshot + identity + rationale.
    h = _Harness(
        conversations={"conv1": _conv()}, bindings={"bind1": _binding("web")}
    )
    n = h.notifier()
    await n.notify_status(_req(rationale="refunding duplicate order"))
    payload = h.web_events[0][2]
    assert payload["mcp_server_name"] == "stripe"
    assert payload["tool_name"] == "charge"
    assert payload["params"] == {"amount": 500}
    assert payload["coworker_id"] == "cw1"
    assert payload["conversation_id"] == "conv1"
    assert payload["rationale"] == "refunding duplicate order"
    assert payload["requested_at"]  # ISO timestamp present
    # A business-policy approval has no safety provenance.
    assert payload["triggered_by"] is None


async def test_web_requested_payload_carries_safety_triggered_by() -> None:
    # A safety-bridge approval forwards triggered_by so the SPA renders the
    # amber "paused by a safety rule" banner from the push alone (§3.10).
    provenance = {
        "kind": "safety_rule",
        "rule_id": "rule-9",
        "check_id": "pii.regex",
        "stage": "pre_tool_call",
    }
    h = _Harness(
        conversations={"conv1": _conv()}, bindings={"bind1": _binding("web")}
    )
    n = h.notifier()
    await n.notify_status(_req(triggered_by=provenance))
    payload = h.web_events[0][2]
    assert payload["triggered_by"] == provenance


async def test_web_card_emits_cancelled_resolution() -> None:
    # §1.5: a container-side cancel flips the web card to ``cancelled``.
    h = _Harness(
        conversations={"conv1": _conv()}, bindings={"bind1": _binding("web")}
    )
    n = h.notifier()
    req = _req()
    await n.notify_status(req)
    await n.notify_hard(req, "cancelled")
    assert h.web_events[1][2] == {
        "type": "approval.resolved",
        "request_id": "req1",
        "outcome": "cancelled",
    }


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
    assert len(h.tg_sends) == 1
    assert h.tg_sends[0][:3] == ("bind1", "chatNew", "req1")


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


# ---------------------------------------------------------------------------
# pending_card_text — the Telegram card body mirrors the web card (spec §6)
# ---------------------------------------------------------------------------

# Telegram's hard cap on a single message. The body must never approach it.
_TELEGRAM_MAX = 4096


def test_card_shows_server_tool_chip_and_each_param() -> None:
    req = _req(
        action={"tool_name": "charge", "params": {"amount": 500, "currency": "usd"}},
    )
    body = pending_card_text(req)
    assert "stripe.charge" in body            # server.tool chip
    assert "amount: 500" in body              # one line per param...
    assert "currency: usd" in body            # ...all of them
    # The original prompt copy survives.
    assert body.startswith("⏳ Approval required for")
    assert "Approve or reject this tool call." in body


def test_card_includes_rationale_line_when_present() -> None:
    req = _req(rationale="refunding a duplicate order")
    body = pending_card_text(req)
    assert "Why: refunding a duplicate order" in body


def test_card_omits_why_line_when_rationale_null() -> None:
    body = pending_card_text(_req(rationale=None))
    assert "Why:" not in body


def test_card_omits_why_line_when_rationale_blank() -> None:
    # Whitespace-only rationale is semantically empty — must not emit "Why:  ".
    body = pending_card_text(_req(rationale="   \n  "))
    assert "Why:" not in body


def test_card_handles_empty_params_gracefully() -> None:
    req = _req(action={"tool_name": "ping", "params": {}})
    body = pending_card_text(req)
    # Chip + prompt still render; no orphan key/value noise, no crash.
    assert "stripe.ping" in body
    assert "Approve or reject this tool call." in body


def test_card_handles_missing_params_key() -> None:
    # A row whose action JSON has no "params" at all must not blow up.
    body = pending_card_text(_req(action={"tool_name": "ping"}))
    assert "stripe.ping" in body


def test_card_handles_non_dict_params() -> None:
    # Malformed-but-valid JSON (params is a list, not an object) — degrade, don't raise.
    body = pending_card_text(_req(action={"tool_name": "ping", "params": ["a", "b"]}))
    assert "stripe.ping" in body
    assert "Approve or reject this tool call." in body


def test_card_handles_missing_tool_name() -> None:
    # No tool_name → chip falls back to the server name alone, no "stripe.None".
    body = pending_card_text(_req(action={"params": {"x": 1}}))
    assert "None" not in body
    assert "stripe" in body


def test_card_clips_an_overlong_value() -> None:
    long_value = "x" * 5000
    req = _req(action={"tool_name": "charge", "params": {"note": long_value}})
    body = pending_card_text(req)
    # The raw 5000-char value must not survive verbatim; the line is clipped.
    assert long_value not in body
    assert "…" in body
    assert len(body) <= _TELEGRAM_MAX


def test_card_truncates_when_params_are_too_many() -> None:
    # Hundreds of params, each non-trivial — the block is dropped past budget
    # and the cut is flagged, with the whole body staying under the cap.
    params = {f"field_{i}": f"value-{i}-" + "y" * 40 for i in range(500)}
    req = _req(action={"tool_name": "charge", "params": params})
    body = pending_card_text(req)
    assert len(body) <= _TELEGRAM_MAX
    assert len(body) <= 2000          # budgeted well under Telegram's limit
    assert "…" in body               # truncation is visible, not silent
    # The prompt must still be intact even after truncation.
    assert "Approve or reject this tool call." in body
    # Not every param could fit.
    rendered = sum(1 for k in params if k in body)
    assert rendered < len(params)


def test_card_is_plain_text_not_markdown() -> None:
    # Param values with Markdown metacharacters are emitted raw (plain text),
    # NOT backslash-escaped — the card is sent without parse_mode.
    req = _req(
        action={"tool_name": "post", "params": {"msg": "_bold_ *x* [a](b) `c`"}},
    )
    body = pending_card_text(req)
    assert "_bold_ *x* [a](b) `c`" in body
    assert "\\_" not in body and "\\*" not in body
