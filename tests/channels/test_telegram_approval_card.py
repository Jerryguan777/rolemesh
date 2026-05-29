"""v6.1 §P2b.1 — Telegram approval card outbound + InlineKeyboardMarkup.

The card path lives on ``_BotInstance.send_approval_card``; testing it
in isolation (without a live Telegram application) needs only a stub
``bot`` whose ``send_message`` we inspect. Mirrors the structure used
in :mod:`tests.channels.test_telegram_start_handler` — keep the seam
narrow so a future refactor of ``_BotInstance`` is forced to update
the contract here, not silently reshape the wire payload.

What we're guarding against (per CLAUDE.md test philosophy — boundary
conditions first):

* The Telegram callback_data byte budget is 64. ``apr:<uuid>`` /
  ``rej:<uuid>`` uses 40, well inside; we still pin the prefix/length
  so a future PR cannot accidentally inflate the payload (e.g. by
  embedding tenant_id) and silently break old clients.
* The card must NOT be sent with Markdown parse_mode — attacker-
  controlled rationale text in the summary could otherwise smuggle
  links into the button area. The plain-text guarantee is one mutation
  away from regressing.
* ``send_approval_card`` is a no-op when the bot hasn't started
  (``_app is None``) so unit tests that don't build a real Application
  don't NPE; an early-return there is what callers depend on.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardMarkup

from rolemesh.approval.notification import ApprovalCardPayload
from rolemesh.channels.telegram_gateway import _BotInstance


def _bot_instance_with_fake_app(send_mock: AsyncMock) -> _BotInstance:
    """Build a _BotInstance whose ``_app.bot.send_message`` is the spy.

    Going through the public constructor + monkey-poking ``_app`` keeps
    the test honest about the surface ``send_approval_card`` reaches
    into. If ``_BotInstance`` adds another required attribute on
    ``_app`` for the card path, this fixture will raise — the test
    fails loudly, not silently.
    """
    async def _on_message_noop(*args: object, **kwargs: object) -> None:
        return None

    inst = _BotInstance(token="dummy-token", on_message=_on_message_noop)
    fake_bot = SimpleNamespace(send_message=send_mock)
    inst._app = SimpleNamespace(bot=fake_bot)  # type: ignore[assignment]
    return inst


@pytest.fixture
def _card() -> ApprovalCardPayload:
    return ApprovalCardPayload(
        request_id="11111111-2222-3333-4444-555555555555",
        title="Approval #11111111",
        summary="erp: 1 action(s)",
        text_fallback="Approval request #11111111 is waiting for review.",
        approval_url="https://example.test/approvals/11111111-2222-3333-4444-555555555555",
    )


async def test_send_approval_card_uses_inline_keyboard_with_apr_rej_callbacks(
    _card: ApprovalCardPayload,
) -> None:
    """T2b.1 — the wire payload carries one InlineKeyboardMarkup row
    with two buttons whose callback_data uses the locked ``apr:`` /
    ``rej:`` prefixes. The inbound handler is the only other place
    that touches these strings; they must stay in lock-step or the
    Phase 2b decision path silently breaks."""
    send_mock = AsyncMock()
    inst = _bot_instance_with_fake_app(send_mock)
    await inst.send_approval_card("chat-42", _card)
    assert send_mock.await_count == 1
    args, kwargs = send_mock.call_args
    # chat_id is positional, body second; reply_markup is the keyword
    # we actually care about.
    chat_id, body = args
    assert chat_id == "chat-42"
    assert "Approval request #11111111" in body
    keyboard = kwargs.get("reply_markup")
    assert isinstance(keyboard, InlineKeyboardMarkup)
    rows = keyboard.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 2
    approve_btn, reject_btn = rows[0]
    assert approve_btn.callback_data == f"apr:{_card.request_id}"
    assert reject_btn.callback_data == f"rej:{_card.request_id}"
    # callback_data must fit in Telegram's 64-byte ceiling. We pick a
    # full-UUID request_id (the live shape) to make this a real check.
    assert len(approve_btn.callback_data.encode("utf-8")) <= 64
    assert len(reject_btn.callback_data.encode("utf-8")) <= 64


async def test_send_approval_card_appends_review_url_when_missing_from_text(
    _card: ApprovalCardPayload,
) -> None:
    """The text fallback on its own is missing the deep-link (formatter
    elides it when ``approval_url`` is None). If a card-capable channel
    still wants to render the URL in body (e.g. user clicks through
    instead of using the button), the gateway appends it. Catches a
    refactor that drops the URL when both paths exist."""
    card = ApprovalCardPayload(
        request_id="abc",
        title="t",
        summary="s",
        text_fallback="Body without URL.",
        approval_url="https://example.test/approvals/abc",
    )
    send_mock = AsyncMock()
    inst = _bot_instance_with_fake_app(send_mock)
    await inst.send_approval_card("c", card)
    body = send_mock.call_args[0][1]
    assert "https://example.test/approvals/abc" in body


async def test_send_approval_card_does_not_double_inject_url(
    _card: ApprovalCardPayload,
) -> None:
    """When the operator-customised fallback already inlines the URL,
    the gateway must NOT append a second copy — the approver would
    otherwise see the deep-link rendered twice."""
    card = ApprovalCardPayload(
        request_id="abc",
        title="t",
        summary="s",
        text_fallback="Body with URL https://example.test/approvals/abc.",
        approval_url="https://example.test/approvals/abc",
    )
    send_mock = AsyncMock()
    inst = _bot_instance_with_fake_app(send_mock)
    await inst.send_approval_card("c", card)
    body = send_mock.call_args[0][1]
    assert body.count("https://example.test/approvals/abc") == 1


async def test_send_approval_card_no_op_before_application_starts(
    _card: ApprovalCardPayload,
) -> None:
    """Reflects the same defensive pattern :meth:`send_message`
    follows: if the underlying Application has not been initialised
    (``_app is None``), the call is a quiet no-op rather than an
    AttributeError. Without this the orchestrator startup race could
    crash on the first approval after a coworker reload."""
    async def _on_message_noop(*args: object, **kwargs: object) -> None:
        return None

    inst = _BotInstance(token="dummy-token", on_message=_on_message_noop)
    # _app stays None.
    await inst.send_approval_card("c", _card)  # must not raise


async def test_send_approval_card_omits_markdown_to_avoid_smuggling(
    _card: ApprovalCardPayload,
) -> None:
    """A mutation that adds ``parse_mode=ParseMode.MARKDOWN`` to the
    card path would let an attacker-controlled summary embed clickable
    links that look like buttons. The card path must send plain text so
    only the explicit InlineKeyboardButton callbacks carry actions."""
    send_mock = AsyncMock()
    inst = _bot_instance_with_fake_app(send_mock)
    await inst.send_approval_card("c", _card)
    kwargs = send_mock.call_args.kwargs
    assert "parse_mode" not in kwargs, (
        "approval card must not opt into Markdown — attacker-controlled "
        "rationale text would otherwise smuggle links into the body"
    )
