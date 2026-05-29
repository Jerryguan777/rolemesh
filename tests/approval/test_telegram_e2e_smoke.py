"""v6.1 §P2b.3 — T2b.9 smoke test: proposal → Telegram card → button
click → engine decision → publisher fan-out.

We do not have a live Telegram bot in CI, but the parts in between
the wire and the engine are real here:

- :class:`ApprovalEngine` with the real Postgres-backed builder.
- :func:`deliver_approval_card_or_text` choosing the card path on a
  spy channel that mimics what the Telegram-capable
  ``_OrchestratorChannelSender`` would do.
- :func:`dispatch_telegram_callback_decision` calling the engine just
  as the real :func:`_handle_approval_callback` would.

Together this exercises everything *except* the python-telegram-bot
adapter (which is its own unit-tested seam in
``tests/channels/test_telegram_approval_card.py``). A real Telegram
bot would only have to relay the captured ``ApprovalCardPayload`` and
the user's click; both ends of that relay are in place.

What this test is supposed to catch (mutation thinking):

* If ``_notify_approvers`` ever reverts to plain
  ``send_to_conversation``, the spy never sees a card → assert fails.
* If the callback dispatcher hard-codes ``outcome='approved'`` for
  both buttons, the resulting DB row would have the wrong status →
  the final ``list_approval_requests`` assertion catches that.
* If ``approval.decided.<id>`` no longer publishes after the engine
  decides, the worker would silently stop firing → the publisher
  spy catches that.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from rolemesh.approval.engine import ApprovalEngine
from rolemesh.approval.notification import (
    ApprovalCardPayload,
    NotificationTargetResolver,
)
from rolemesh.channels.telegram_gateway import (
    dispatch_telegram_callback_decision,
    set_approval_decision_router,
)
from rolemesh.db import (
    create_approval_policy,
    create_channel_binding,
    create_channel_identity,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    list_approval_requests,
)

pytestmark = pytest.mark.usefixtures("test_db")


class _Publisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publishes.append((subject, data))


class _TelegramLikeChannel:
    """A channel-sender stand-in that exposes the card path the way
    the real ``_OrchestratorChannelSender`` will when its Telegram
    gateway is wired (Phase 2b).

    We deliberately implement BOTH ``send_to_conversation`` and
    ``send_approval_card`` so the dispatcher chooses the card path,
    and we record what each saw so the assertions can distinguish a
    card from a plain text fallback (the regression we are guarding
    against)."""

    def __init__(self) -> None:
        self.cards: list[tuple[str, ApprovalCardPayload]] = []
        self.texts: list[tuple[str, str]] = []

    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        self.texts.append((conversation_id, text))

    async def send_approval_card(
        self, conversation_id: str, card: ApprovalCardPayload
    ) -> None:
        self.cards.append((conversation_id, card))


async def _seed_full_state(
    *, bot_token: str, sender_id: str
) -> tuple[str, str, str, str]:
    """Set up tenant/user/coworker/binding/conv + telegram identity +
    matching approval policy. Returns the IDs the test needs to drive
    the engine."""
    suffix = uuid.uuid4().hex[:6]
    t = await create_tenant(name="T", slug=f"e2e-{suffix}")
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{suffix}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{suffix}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": bot_token},
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    await create_approval_policy(
        tenant_id=t.id,
        coworker_id=cw.id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"field": "amount", "op": ">", "value": 1000},
        approver_user_ids=[u.id],
        priority=0,
    )
    await create_channel_identity(t.id, "telegram", sender_id, u.id)
    return t.id, u.id, cw.id, conv.id


def _resolver_returning(conv_id: str) -> NotificationTargetResolver:
    async def _convs(_user_id: str, _coworker_id: str) -> list[str]:
        return [conv_id]

    async def _get_conv(_conv_id: str) -> object | None:
        return object()

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_get_conv,
    )


async def test_proposal_to_telegram_button_to_decided() -> None:
    """T2b.9 — the full happy path:

    1. Agent submits a matching proposal.
    2. Engine creates ``pending`` row and notifies the approver. The
       spy channel receives a structured ``ApprovalCardPayload`` — the
       seam Telegram uses to render InlineKeyboardMarkup.
    3. User clicks ✅ → the gateway's dispatcher calls
       :meth:`ApprovalEngine.handle_decision`.
    4. Row transitions to ``approved`` and the engine publishes
       ``approval.decided.<request_id>`` so the Worker can pick it up.
    """
    bot_token = f"tkn-{uuid.uuid4().hex[:8]}"
    sender_id = "919191"
    tenant_id, user_id, cw_id, conv_id = await _seed_full_state(
        bot_token=bot_token, sender_id=sender_id
    )

    pub = _Publisher()
    ch = _TelegramLikeChannel()
    engine = ApprovalEngine(
        publisher=pub,
        channel_sender=ch,
        resolver=_resolver_returning(conv_id),
    )

    # Step 1: agent → proposal
    await engine.handle_proposal(
        {
            "tenantId": tenant_id,
            "coworkerId": cw_id,
            "conversationId": conv_id,
            "jobId": "job-1",
            "userId": user_id,
            "rationale": "refund for big order",
            "actions": [
                {
                    "mcp_server": "erp",
                    "tool_name": "refund",
                    "params": {"amount": 5000},
                }
            ],
        },
        tenant_id=tenant_id,
        coworker_id=cw_id,
    )

    # Step 2: approver receives a CARD (not text). This is the seam
    # that Phase 2a's dispatcher added and Phase 2b's engine commit
    # actually consumes; without the §P2b.1 wiring this list would
    # still be empty.
    assert ch.cards, "engine must deliver via the card path for §P2b.1"
    assert ch.texts == [], (
        "card-capable channel must not also receive text — that would "
        "double-notify the approver"
    )
    delivered_conv, card = ch.cards[0]
    assert delivered_conv == conv_id

    reqs = await list_approval_requests(tenant_id)
    assert len(reqs) == 1
    assert reqs[0].status == "pending"
    assert card.request_id == reqs[0].id, (
        "card must carry the engine-assigned request_id so the inbound "
        "callback can route it back without a second DB lookup"
    )

    # Step 3: simulate the user tapping ✅. The dispatcher exercises
    # the exact bot_token → tenant → user resolution chain the real
    # CallbackQueryHandler uses.
    set_approval_decision_router(engine)
    try:
        result = await dispatch_telegram_callback_decision(
            bot_token=bot_token,
            sender_id=sender_id,
            callback_data=f"apr:{card.request_id}",
        )
    finally:
        set_approval_decision_router(None)
    assert result.kind == "approved"

    # Step 4: row is approved + decided event published.
    reqs = await list_approval_requests(tenant_id)
    assert len(reqs) == 1
    assert reqs[0].status == "approved", (
        "engine did not flip the row to approved on the button click"
    )
    decided_subjects = [s for (s, _d) in pub.publishes if s.startswith("approval.decided.")]
    assert decided_subjects == [
        f"approval.decided.{card.request_id}"
    ], (
        "engine must publish approval.decided so the Worker picks up "
        "the action; missing event means execution stalls"
    )
    # The decided payload should carry the tenant + new status so the
    # Worker can scope its subsequent claims. Asserting the body shape
    # protects against an accidental schema regression in the
    # decided-publisher format.
    decided_body = json.loads(
        next(d for (s, d) in pub.publishes if s == decided_subjects[0])
    )
    assert decided_body.get("tenant_id") == tenant_id
    assert decided_body.get("status") == "approved"


async def test_proposal_to_reject_via_telegram_callback() -> None:
    """Same shape as the happy path, but the user taps ❌. Pinning
    both outcomes together keeps the assertion meaningful: a mutation
    that hard-codes outcome='approved' would still pass the happy
    path while failing here."""
    bot_token = f"tkn-{uuid.uuid4().hex[:8]}"
    sender_id = "929292"
    tenant_id, user_id, cw_id, conv_id = await _seed_full_state(
        bot_token=bot_token, sender_id=sender_id
    )

    pub = _Publisher()
    ch = _TelegramLikeChannel()
    engine = ApprovalEngine(
        publisher=pub,
        channel_sender=ch,
        resolver=_resolver_returning(conv_id),
    )

    await engine.handle_proposal(
        {
            "tenantId": tenant_id,
            "coworkerId": cw_id,
            "conversationId": conv_id,
            "jobId": "job-2",
            "userId": user_id,
            "rationale": "refund",
            "actions": [
                {
                    "mcp_server": "erp",
                    "tool_name": "refund",
                    "params": {"amount": 5000},
                }
            ],
        },
        tenant_id=tenant_id,
        coworker_id=cw_id,
    )
    assert ch.cards
    card = ch.cards[0][1]

    set_approval_decision_router(engine)
    try:
        result = await dispatch_telegram_callback_decision(
            bot_token=bot_token,
            sender_id=sender_id,
            callback_data=f"rej:{card.request_id}",
        )
    finally:
        set_approval_decision_router(None)
    assert result.kind == "rejected"

    reqs = await list_approval_requests(tenant_id)
    assert reqs[0].status == "rejected"
    decided_subjects = [s for (s, _) in pub.publishes if s.startswith("approval.decided.")]
    decided_body: dict[str, Any] = json.loads(
        next(d for (s, d) in pub.publishes if s == decided_subjects[0])
    )
    assert decided_body.get("status") == "rejected"


async def test_owner_fyi_path_does_not_send_card() -> None:
    """T2b.8 — the owner-FYI edge path must keep going through
    plain ``send_to_conversation``. Surfacing a button there would
    invite owners to "decide" something with no DB row, which is
    exactly the confusion the §P2.6 design carved out to avoid."""
    bot_token = f"tkn-{uuid.uuid4().hex[:8]}"
    sender_id = "939393"
    tenant_id, _user_id, cw_id, conv_id = await _seed_full_state(
        bot_token=bot_token, sender_id=sender_id
    )

    pub = _Publisher()
    ch = _TelegramLikeChannel()
    engine = ApprovalEngine(
        publisher=pub,
        channel_sender=ch,
        resolver=_resolver_returning(conv_id),
    )

    # auto_intercept with empty userId triggers the E path; the engine
    # fans out FYI text to tenant owners' conversations.
    await engine.handle_auto_intercept(
        {
            "tenantId": tenant_id,
            "coworkerId": cw_id,
            "conversationId": conv_id,
            "jobId": "j-edge",
            "userId": "",  # the v6.1 edge signal
            "mcp_server_name": "erp",
            "tool_name": "refund",
            "tool_params": {"amount": 5000},
            "action_hash": "h-edge",
        },
        tenant_id=tenant_id,
        coworker_id=cw_id,
    )
    # No DB row created on the edge path (invariant #6).
    assert await list_approval_requests(tenant_id) == []
    # Owner got a plain text — NOT a card.
    assert ch.cards == [], (
        "owner FYI must not surface a button; there is no row to "
        "decide on"
    )
    assert ch.texts, "owner FYI text must still reach the owner"
    body = ch.texts[0][1]
    assert "FYI" in body
