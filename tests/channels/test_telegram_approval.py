"""Telegram HITL approval card + callback (docs/21-hitl-approval-plan.md §10 S4).

Exercises the module-level parse/dispatch helpers with a stub ``Update`` — no
live ``Application``. The IDOR-relevant property under test: the callback never
carries approver identity, only ``request_id`` + verb, and the authenticated
``from_user.id`` is what flows to the orchestrator funnel.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from rolemesh.channels.telegram_gateway import (
    _APPROVE_PREFIX,
    _REJECT_PREFIX,
    _approval_keyboard,
    _handle_approval_callback,
    parse_approval_callback,
)

# ---------------------------------------------------------------------------
# callback_data parsing
# ---------------------------------------------------------------------------


def test_parse_approve_and_reject() -> None:
    assert parse_approval_callback("apr:abc-123") == ("approve", "abc-123")
    assert parse_approval_callback("rej:abc-123") == ("reject", "abc-123")


def test_parse_rejects_malformed() -> None:
    assert parse_approval_callback(None) is None
    assert parse_approval_callback("") is None
    assert parse_approval_callback("apr:") is None          # empty request_id
    assert parse_approval_callback("rej:") is None
    assert parse_approval_callback("other:x") is None       # foreign keyboard
    assert parse_approval_callback("approve:x") is None      # not our prefix


def test_callback_data_stays_within_telegram_limit() -> None:
    # 36-char UUID + 4-char prefix = 40 bytes, well under Telegram's 64-byte cap.
    rid = "123e4567-e89b-12d3-a456-426614174000"
    assert len(f"{_APPROVE_PREFIX}{rid}") <= 64
    assert len(f"{_REJECT_PREFIX}{rid}") <= 64


def test_keyboard_carries_request_id_in_callback_data() -> None:
    kb = _approval_keyboard("req-9")
    buttons = kb.inline_keyboard[0]
    datas = {b.callback_data for b in buttons}
    assert datas == {"apr:req-9", "rej:req-9"}


# ---------------------------------------------------------------------------
# callback dispatch
# ---------------------------------------------------------------------------


def _update(data: str | None, *, user_id: int = 7, chat_id: int = 99) -> Any:
    answered: dict[str, Any] = {}

    async def _answer(text: str | None = None) -> None:
        answered["called"] = True
        answered["text"] = text

    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
        answer=_answer,
    )
    return SimpleNamespace(callback_query=query), answered


async def test_valid_tap_dispatches_authenticated_identity() -> None:
    calls: list[tuple[str, str, str, str]] = []

    async def _on_decision(
        request_id: str, decision: str, telegram_user_id: str, chat_id: str
    ) -> str | None:
        calls.append((request_id, decision, telegram_user_id, chat_id))
        return "Approved ✅"

    update, answered = _update("apr:req1", user_id=7, chat_id=99)
    await _handle_approval_callback(update, _on_decision)

    # request_id + verb from callback_data; identity from the authenticated
    # Telegram sender (NOT from any client-controlled field).
    assert calls == [("req1", "approve", "7", "99")]
    assert answered["text"] == "Approved ✅"


async def test_malformed_tap_does_not_dispatch_but_answers() -> None:
    calls: list[Any] = []

    async def _on_decision(*a: Any) -> str | None:
        calls.append(a)
        return None

    update, answered = _update("garbage")
    await _handle_approval_callback(update, _on_decision)
    assert calls == []                 # no bogus decision
    assert answered.get("called") is True  # spinner still cleared


async def test_dispatch_error_still_answers_with_retry_toast() -> None:
    async def _on_decision(*_a: Any) -> str | None:
        raise RuntimeError("orchestrator down")

    update, answered = _update("rej:req1")
    await _handle_approval_callback(update, _on_decision)
    assert answered["called"] is True
    assert "retry" in (answered["text"] or "").lower()


async def test_no_decision_callback_is_safe() -> None:
    update, answered = _update("apr:req1")
    await _handle_approval_callback(update, None)
    assert answered["called"] is True
