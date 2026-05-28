"""v6.1 §P1.5 / §P1.6 — admission gate behaviour.

The gate has two halves:
- ``_short_circuit_group`` in ``telegram_gateway.py`` drops every
  group/supergroup/channel inbound at the gateway with one guidance
  reply (T1.9).
- ``admit_telegram_1on1`` in ``channels/admission.py`` resolves the
  sender; on a miss it sends the unified guidance via the gateway
  and returns ``None`` so the orchestrator can drop the message
  (T1.7). On a hit it returns the RoleMesh user_id (used by C4 for
  the lazy backfill of ``conv.user_id``).

Tests use real Postgres for the resolve path and a stub gateway for
the wire interactions. The orchestrator-side glue in main.py is
exercised separately (heavier integration); this file pins the
helpers themselves.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rolemesh.channels.admission import (
    ADMISSION_GUIDE_TEXT,
    GROUP_NOT_SUPPORTED_TEXT,
    admit_telegram_1on1,
)
from rolemesh.channels.telegram_gateway import _short_circuit_group
from rolemesh.db import (
    create_channel_identity,
    create_tenant,
    create_user,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# T1.9 — Telegram gateway group short-circuit
# ---------------------------------------------------------------------------


def _make_chat_update(chat_type: str) -> tuple[object, AsyncMock]:
    send = AsyncMock()
    chat = SimpleNamespace(id=42, type=chat_type, send_message=send)
    update = SimpleNamespace(effective_chat=chat)
    return update, send


async def test_short_circuit_replies_and_returns_true_for_group() -> None:
    update, send = _make_chat_update("group")
    handled = await _short_circuit_group(update)
    assert handled is True
    send.assert_awaited_once_with(GROUP_NOT_SUPPORTED_TEXT)


async def test_short_circuit_replies_and_returns_true_for_supergroup() -> None:
    update, send = _make_chat_update("supergroup")
    handled = await _short_circuit_group(update)
    assert handled is True
    send.assert_awaited_once_with(GROUP_NOT_SUPPORTED_TEXT)


async def test_short_circuit_replies_and_returns_true_for_channel() -> None:
    """``channel`` is included defensively — design §P1.5 mandates the
    three-type set. A future near-miss mutation that narrows to just
    ``(group, supergroup)`` would slip channels through to admission."""
    update, send = _make_chat_update("channel")
    handled = await _short_circuit_group(update)
    assert handled is True
    send.assert_awaited_once_with(GROUP_NOT_SUPPORTED_TEXT)


async def test_short_circuit_does_not_fire_for_private_chat() -> None:
    update, send = _make_chat_update("private")
    handled = await _short_circuit_group(update)
    assert handled is False
    send.assert_not_awaited()


async def test_short_circuit_handles_missing_chat_defensively() -> None:
    update = SimpleNamespace(effective_chat=None)
    assert await _short_circuit_group(update) is False


async def test_short_circuit_swallows_send_failure_but_still_drops() -> None:
    """A transient Telegram API failure on the guidance reply must
    NOT cause the group message to slip through to ``on_message``.
    Drop posture is fail-closed regardless of whether the user saw
    the reply.
    """
    send = AsyncMock(side_effect=RuntimeError("Telegram API down"))
    chat = SimpleNamespace(id=42, type="group", send_message=send)
    update = SimpleNamespace(effective_chat=chat)
    handled = await _short_circuit_group(update)
    assert handled is True  # still drops even when the reply failed.


# ---------------------------------------------------------------------------
# T1.7 — admit_telegram_1on1 ↔ DB
# ---------------------------------------------------------------------------


async def _seed_user(slug_tag: str) -> tuple[str, str]:
    t = await create_tenant(
        name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}"
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
    )
    return t.id, u.id


def _stub_gateway() -> tuple[object, AsyncMock]:
    send = AsyncMock()
    gateway = SimpleNamespace(send_message=send)
    return gateway, send


async def test_admit_returns_user_id_on_hit_and_does_not_reply() -> None:
    """Happy path: linked sender resolves to user_id, no guidance
    sent, ready for the caller to continue processing.
    """
    tid, uid = await _seed_user("admit-hit")
    await create_channel_identity(tid, "telegram", "555", uid)
    gateway, send = _stub_gateway()
    resolved = await admit_telegram_1on1(
        tenant_id=tid,
        sender_channel_id="555",
        gateway=gateway,
        binding_id="bnd-123",
        chat_id="chat-abc",
    )
    assert resolved == uid
    send.assert_not_awaited()


async def test_admit_replies_guidance_and_returns_none_on_miss() -> None:
    """T1.7 — Unlinked sender: gate denies and the gateway is asked
    to send the unified guidance text. Returns None so the caller
    short-circuits before storing the message.
    """
    tid, _ = await _seed_user("admit-miss")
    gateway, send = _stub_gateway()
    resolved = await admit_telegram_1on1(
        tenant_id=tid,
        sender_channel_id="strangers-id",
        gateway=gateway,
        binding_id="bnd-xyz",
        chat_id="chat-def",
    )
    assert resolved is None
    # The guidance MUST go to the right binding + chat; a typo here
    # would deliver the message to nowhere or — worse — to another
    # conversation.
    send.assert_awaited_once_with(
        "bnd-xyz", "chat-def", ADMISSION_GUIDE_TEXT
    )


async def test_admit_normalises_channel_id_via_caller_contract() -> None:
    """The TEXT-keyed lookup means the caller MUST normalise to a
    string. We do not coerce here — calling with an int would
    silently miss (the SQL parameter type would still match TEXT via
    asyncpg encoding but the *value* equality would not), so this
    test pins the caller-side contract.
    """
    tid, uid = await _seed_user("admit-norm")
    await create_channel_identity(tid, "telegram", "777", uid)
    gateway, _send = _stub_gateway()
    # Same numeric digits as a string — must hit.
    resolved = await admit_telegram_1on1(
        tenant_id=tid,
        sender_channel_id="777",
        gateway=gateway,
        binding_id="bnd",
        chat_id="chat",
    )
    assert resolved == uid


async def test_admit_still_denies_when_guidance_send_fails() -> None:
    """If the gateway raises while sending the guidance reply, the
    helper still returns None so the orchestrator drops the message
    — the user's experience degrades to "I didn't get a guidance
    reply", but admission posture stays fail-closed.
    """
    tid, _ = await _seed_user("admit-fail")
    send = AsyncMock(side_effect=RuntimeError("send blew up"))
    gateway = SimpleNamespace(send_message=send)
    resolved = await admit_telegram_1on1(
        tenant_id=tid,
        sender_channel_id="ghost-2",
        gateway=gateway,
        binding_id="bnd",
        chat_id="chat",
    )
    assert resolved is None
    send.assert_awaited_once()


async def test_admit_scoped_by_tenant() -> None:
    """A sender linked under tenant A must NOT be admitted in tenant
    B. Tenant scoping is enforced by the WHERE clause; this test
    catches a mutation that drops the tenant_id predicate.
    """
    tid_a, uid_a = await _seed_user("scope-a")
    tid_b, _ = await _seed_user("scope-b")
    await create_channel_identity(tid_a, "telegram", "shared", uid_a)
    gateway, send = _stub_gateway()
    # Probing from tenant B must miss.
    resolved = await admit_telegram_1on1(
        tenant_id=tid_b,
        sender_channel_id="shared",
        gateway=gateway,
        binding_id="bnd",
        chat_id="chat",
    )
    assert resolved is None
    send.assert_awaited_once_with("bnd", "chat", ADMISSION_GUIDE_TEXT)
