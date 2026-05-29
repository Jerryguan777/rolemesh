"""Unit tests for the notification module's shaping + dispatch.

The module's other contracts (target resolution chain, formatter
strings) are exercised through the engine tests; this file pins the
v6.1 §P2.7 card-vs-text dispatcher because it has its own branching
that the engine tests do not cover.
"""

from __future__ import annotations

import pytest

from rolemesh.approval.notification import (
    ApprovalCardPayload,
    deliver_approval_card_or_text,
)

pytestmark = pytest.mark.usefixtures("test_db")


class _TextOnlyChannel:
    """Default fallback case: no ``send_approval_card`` method."""

    def __init__(self) -> None:
        self.text_sent: list[tuple[str, str]] = []

    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        self.text_sent.append((conversation_id, text))


class _CardCapableChannel:
    """Phase 2b-style channel: prefers the card path."""

    def __init__(self) -> None:
        self.cards_sent: list[tuple[str, ApprovalCardPayload]] = []
        self.text_sent: list[tuple[str, str]] = []

    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        self.text_sent.append((conversation_id, text))

    async def send_approval_card(
        self, conversation_id: str, card: ApprovalCardPayload
    ) -> None:
        self.cards_sent.append((conversation_id, card))


@pytest.fixture
def _card() -> ApprovalCardPayload:
    return ApprovalCardPayload(
        request_id="req-1",
        title="Approval needed",
        summary="erp/refund",
        text_fallback="Approval #req-1 awaits review.",
        approval_url="https://example.test/approvals/req-1",
    )


async def test_dispatcher_uses_card_method_when_available(
    _card: ApprovalCardPayload,
) -> None:
    """T2a (§P2.7) — channels that opt in via ``send_approval_card``
    receive the structured payload, not the text fallback. Catches a
    refactor that drops the hasattr branch."""
    ch = _CardCapableChannel()
    await deliver_approval_card_or_text(ch, "conv-1", _card)
    assert ch.cards_sent == [("conv-1", _card)]
    assert ch.text_sent == [], (
        "card-capable channel must not also receive the text fallback"
    )


async def test_dispatcher_falls_back_to_text_with_url(
    _card: ApprovalCardPayload,
) -> None:
    """T2a (§P2.7) — channels without ``send_approval_card`` receive
    the text fallback. The Web URL must be appended (non-Web channels
    have no way to render the button otherwise)."""
    ch = _TextOnlyChannel()
    await deliver_approval_card_or_text(ch, "conv-2", _card)
    assert len(ch.text_sent) == 1
    conv_id, body = ch.text_sent[0]
    assert conv_id == "conv-2"
    assert _card.text_fallback in body
    assert _card.approval_url in body, (
        "the text fallback must surface the Web approval URL so "
        "non-card channels can still route the user"
    )


async def test_dispatcher_does_not_duplicate_url_when_already_present() -> None:
    """T2a — defensive: if the fallback text already includes the URL
    (operator-customised template), the dispatcher must not append it
    a second time. Without this guard the user would see two copies."""
    card = ApprovalCardPayload(
        request_id="req-2",
        title="t",
        summary="s",
        text_fallback="Approve at https://example.test/approvals/req-2",
        approval_url="https://example.test/approvals/req-2",
    )
    ch = _TextOnlyChannel()
    await deliver_approval_card_or_text(ch, "conv-3", card)
    body = ch.text_sent[0][1]
    assert body.count("https://example.test/approvals/req-2") == 1


async def test_dispatcher_omits_url_when_unconfigured() -> None:
    """T2a — graceful when ``approval_url`` is None (dev / unconfigured
    webui base URL). The fallback text alone is delivered; no
    'None' or empty URL is rendered."""
    card = ApprovalCardPayload(
        request_id="req-3",
        title="t",
        summary="s",
        text_fallback="Approve via Web.",
        approval_url=None,
    )
    ch = _TextOnlyChannel()
    await deliver_approval_card_or_text(ch, "conv-4", card)
    body = ch.text_sent[0][1]
    assert body == "Approve via Web."
