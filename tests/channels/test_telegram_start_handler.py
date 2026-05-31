"""v6.1 §P1.4 — Telegram ``/start [<token>]`` command handler.

The handler is the inbound leg of the WebUI link flow; testing it
in isolation (without spinning up a live ``telegram.Application``)
needs only a stubbed ``Update`` / ``Context`` shape. The DB side is
real, so the atomic-consume + UNIQUE behaviour exercised here is the
same code path the running gateway uses.

What we're guarding against — drawn from the design + CLAUDE.md test
philosophy:
- The token rejection message must not leak whether the token was
  unknown, expired, or already used (same wire text in all three).
- ``channel_id`` is normalised to ``str(update.effective_user.id)``
  even when Telegram delivers ``from.id`` as an int. Without this
  normalisation, the resolve path (Phase 1 Checkpoint 4) would miss.
- Token MUST be consumed even when the subsequent identity INSERT
  collides — otherwise a leaked + replayed token could re-attempt
  binding indefinitely.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rolemesh.channels.admission import (
    LINK_ALREADY_BOUND_TEXT,
    LINK_MISSING_TOKEN_TEXT,
    LINK_REJECTED_TEXT,
    LINK_SUCCESS_PREFIX,
)
from rolemesh.channels.telegram_gateway import _handle_start_command
from rolemesh.db import (
    _get_admin_pool,
    consume_link_token,
    create_channel_identity,
    create_link_token,
    create_tenant,
    create_user,
    list_channel_identities_for_user,
)

pytestmark = pytest.mark.usefixtures("test_db")


def _make_update(
    *, telegram_user_id: int, first_name: str = "Jerry", args: list[str] | None = None
) -> tuple[object, object, AsyncMock]:
    """Build minimal Update + Context stubs that match the slice of the
    PTB surface ``_handle_start_command`` reaches into. Returns the
    update, the context, and the ``chat.send_message`` mock so tests
    can assert on the wire text directly.
    """
    send_mock = AsyncMock()
    chat = SimpleNamespace(send_message=send_mock)
    user = SimpleNamespace(
        id=telegram_user_id, first_name=first_name, username=None
    )
    update = SimpleNamespace(effective_chat=chat, effective_user=user)
    context = SimpleNamespace(args=args or [])
    return update, context, send_mock


async def _seed_user(slug_tag: str) -> tuple[str, str]:
    t = await create_tenant(
        name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}"
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
    )
    return t.id, u.id


# ---------------------------------------------------------------------------
# No / malformed args
# ---------------------------------------------------------------------------


async def test_start_with_no_args_replies_guidance_and_does_not_consume() -> None:
    """A bare ``/start`` (often the first message a brand-new Telegram
    user sends to a bot) must point them at the Web flow instead of
    silently doing nothing or leaking that the bot accepts tokens.
    """
    update, context, send_mock = _make_update(telegram_user_id=42)
    await _handle_start_command(update, context)
    send_mock.assert_awaited_once_with(LINK_MISSING_TOKEN_TEXT)


# ---------------------------------------------------------------------------
# Token rejection paths — unified error message
# ---------------------------------------------------------------------------


async def test_start_with_unknown_token_replies_generic_rejection() -> None:
    """Unknown / not-our-table tokens get the same reply as expired
    ones. This is a *defensive* property: an attacker probing tokens
    must not be able to discriminate ``unknown`` from ``expired``
    from ``already used`` by reading the response.
    """
    update, context, send_mock = _make_update(
        telegram_user_id=42,
        args=["bogus-" + uuid.uuid4().hex],
    )
    await _handle_start_command(update, context)
    send_mock.assert_awaited_once_with(LINK_REJECTED_TEXT)


async def test_start_with_expired_token_replies_generic_rejection() -> None:
    """Same rejection wire text for the expired branch."""
    tid, uid = await _seed_user("expired")
    token, _ = await create_link_token(uid, tid, "telegram", ttl_seconds=-1)
    update, context, send_mock = _make_update(
        telegram_user_id=99, args=[token]
    )
    await _handle_start_command(update, context)
    send_mock.assert_awaited_once_with(LINK_REJECTED_TEXT)


async def test_start_with_already_used_token_replies_generic_rejection() -> None:
    """A replay of a previously consumed token must not leak that fact
    — wire text matches the unknown/expired branches.
    """
    tid, uid = await _seed_user("replay")
    token, _ = await create_link_token(uid, tid, "telegram")
    first = await consume_link_token(token)
    assert first is not None  # baseline: first use succeeded
    # The handler ought to also get None for replay.
    update, context, send_mock = _make_update(
        telegram_user_id=99, args=[token]
    )
    await _handle_start_command(update, context)
    send_mock.assert_awaited_once_with(LINK_REJECTED_TEXT)


# ---------------------------------------------------------------------------
# Success path — identity row + channel_id normalisation
# ---------------------------------------------------------------------------


async def test_start_happy_path_writes_identity_and_normalises_channel_id() -> None:
    """Telegram delivers ``from.id`` as an int; the handler stores it
    as ``str(int)``. If a future hand-edit drops the ``str(...)`` cast
    the resolve helper in Checkpoint 4 would miss the row (the lookup
    key is TEXT). This test pins the cast.
    """
    tid, uid = await _seed_user("happy")
    token, _ = await create_link_token(uid, tid, "telegram")
    update, context, send_mock = _make_update(
        telegram_user_id=10101, first_name="Alice", args=[token]
    )
    await _handle_start_command(update, context)
    # Wire reply confirms.
    [(reply_args, _kw)] = [
        (c.args, c.kwargs) for c in send_mock.await_args_list
    ]
    assert LINK_SUCCESS_PREFIX in reply_args[0]
    # DB shape: one row, channel_id is the digits-as-string form.
    links = await list_channel_identities_for_user(uid, tid)
    assert len(links) == 1
    assert links[0].channel_id == "10101"
    assert links[0].platform == "telegram"


async def test_start_unique_violation_branch_still_marks_token_used() -> None:
    """If two RoleMesh users hand the same Telegram account a fresh
    link-token each (e.g. office IT mistakenly used Person A's
    account to test Person B's flow), the second /start must:
    - reject the second binding,
    - still consume the second token (so it cannot be replayed by a
      third party who sniffed it).

    Without "consume even on UNIQUE violation" a leaked token could
    keep being retried after each unbind/rebind cycle.
    """
    tid, uid_a = await _seed_user("dup-a")
    _, uid_b = await _seed_user("dup-b")
    # User A is already bound to this Telegram account.
    await create_channel_identity(tid, "telegram", "555", uid_a)
    # User B (somehow same tenant) tries to bind the same account.
    token_b, _ = await create_link_token(uid_b, tid, "telegram")
    update, context, send_mock = _make_update(
        telegram_user_id=555, args=[token_b]
    )
    await _handle_start_command(update, context)
    send_mock.assert_awaited_once_with(LINK_ALREADY_BOUND_TEXT)
    # The token must be unusable now — replay attempt yields None.
    replay = await consume_link_token(token_b)
    assert replay is None, "token must be marked used on UNIQUE branch too"
    # A's link is still intact (no DB corruption).
    a_links = await list_channel_identities_for_user(uid_a, tid)
    assert {l.channel_id for l in a_links} == {"555"}


async def test_start_missing_chat_or_user_no_op() -> None:
    """If Telegram delivers an Update without effective_chat or
    effective_user (defensive — should not happen, but PTB types
    allow None), the handler returns cleanly and does not touch DB.
    """
    update = SimpleNamespace(effective_chat=None, effective_user=None)
    context = SimpleNamespace(args=["x"])
    # Must not raise.
    await _handle_start_command(update, context)
    # And no link_tokens / identity rows were written.
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT 1 FROM user_channel_identities LIMIT 1"
        )
        assert rows == []
