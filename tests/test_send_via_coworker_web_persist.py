"""``_send_via_coworker`` must persist web assistant replies to DB.

Regression: scheduled-task replies (and any other IPC-driven send
that goes through ``_send_via_coworker``) used to only publish on
the NATS ``web.outbound.*`` subject. The webui's WS consumer uses
``DeliverPolicy.NEW``, so if the user's browser was not subscribed
at the exact moment the task fired, the message was lost — and
nothing was written to the ``messages`` table either, so reloading
the page didn't bring it back. Telegram replies happened to "work"
because the Telegram server persists chat history independently;
web has no such fallback.

The contract pinned here: when ``_send_via_coworker`` dispatches via
the web gateway, it MUST also write the message to ``messages`` so
that conversation history reflects the delivery, independent of WS
connectivity at fire time.

The non-web negative path is pinned in the same file so a future
refactor that accidentally over-persists (e.g. double-storing
Telegram replies that already round-trip through the platform's own
history) surfaces immediately.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

import rolemesh.main as m
from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerState,
    OrchestratorState,
)
from rolemesh.db import (
    _get_admin_pool,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _RecordingGateway:
    """Stand-in for a real ChannelGateway. Records send_message calls
    so the test can verify the NATS-side delivery still fires (this
    fix must NOT replace the publish with a DB write — it must do
    both).
    """

    def __init__(self) -> None:
        self.sends: list[tuple[str, str, str]] = []

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        self.sends.append((binding_id, chat_id, text))


async def _query_messages(conversation_id: str) -> list[dict[str, object]]:
    """Read messages for a conversation via the admin pool (RLS-bypass).

    Tests assert directly against the rows the orchestrator wrote
    rather than through ``get_messages_since`` so the assertions
    survive any future change to the read API's filtering.
    """
    pool = await _get_admin_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, sender, sender_name, content, is_from_me, "
            "is_bot_message FROM messages WHERE conversation_id = $1::uuid "
            "ORDER BY timestamp",
            conversation_id,
        )
    return [dict(r) for r in rows]


@asynccontextmanager
async def _patched_state_and_gateways(
    *,
    state: OrchestratorState,
    gateways: dict[str, object],
) -> AsyncIterator[None]:
    """Swap module globals for the duration of one test, then restore.

    ``_send_via_coworker`` reads both ``_state`` and ``_gateways`` from
    module scope, so a clean test needs to patch both. The yield-then-
    restore pattern keeps a failing assertion from leaking the patches
    into neighbouring tests.
    """
    orig_state = m._state
    orig_gateways = m._gateways
    m._state = state  # type: ignore[assignment]
    m._gateways = gateways  # type: ignore[assignment]
    try:
        yield
    finally:
        m._state = orig_state  # type: ignore[assignment]
        m._gateways = orig_gateways  # type: ignore[assignment]


async def _seed_web_conv(slug_tag: str) -> tuple[CoworkerState, str, str]:
    """Create a tenant + coworker + web channel binding + conversation.

    Returns (coworker_state_seeded_with_binding_and_conv,
             channel_chat_id, conversation_id).
    """
    t = await create_tenant(name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id,
        name="Adam",
        folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    binding = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="web",
    )
    chat_id = f"web-chat-{uuid.uuid4().hex[:8]}"
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=binding.id,
        channel_chat_id=chat_id,
    )

    cw_state = CoworkerState.from_coworker(cw)
    cw_state.channel_bindings["web"] = binding
    cw_state.conversations[conv.id] = ConversationState(conversation=conv)
    return cw_state, chat_id, conv.id


# ---------------------------------------------------------------------------
# Positive: web reply on the primary (cw_state given) path
# ---------------------------------------------------------------------------


async def test_web_reply_via_primary_path_lands_in_messages_table() -> None:
    """The scheduled-task IPC handler calls ``_send_via_coworker`` with
    the resolved coworker_state. After the call, the web conversation's
    history must contain the reply — independent of whether any WS
    was subscribed when the publish happened.
    """
    cw_state, chat_id, conv_id = await _seed_web_conv("primary")
    gateway = _RecordingGateway()
    state = OrchestratorState()
    state.coworkers[cw_state.config.id] = cw_state

    async with _patched_state_and_gateways(
        state=state, gateways={"web": gateway}
    ):
        await m._send_via_coworker(cw_state, chat_id, "⏰ 2 分钟到啦")

    rows = await _query_messages(conv_id)
    assert len(rows) == 1, (
        f"expected exactly one persisted message, got {len(rows)}: {rows}"
    )
    row = rows[0]
    assert row["content"] == "⏰ 2 分钟到啦"
    assert row["sender"] == "Adam"
    assert row["sender_name"] == "Adam"
    assert row["is_from_me"] is True
    assert row["is_bot_message"] is True

    assert gateway.sends == [(cw_state.channel_bindings["web"].id, chat_id, "⏰ 2 分钟到啦")], (
        "DB persistence must not have replaced the NATS publish — both "
        "channels deliver"
    )


# ---------------------------------------------------------------------------
# Positive: web reply on the fallback (cw_state=None → scan _state) path
# ---------------------------------------------------------------------------


async def test_web_reply_via_fallback_path_also_persists() -> None:
    """Some callers (e.g. ``_IpcDepsImpl.send_message``) invoke
    ``_send_via_coworker`` with ``cw_state=None`` and rely on the scan
    over ``_state.coworkers`` to find the conversation. Both branches
    must persist or you get a "works for scheduled tasks, breaks for
    proposal callbacks" split.
    """
    cw_state, chat_id, conv_id = await _seed_web_conv("fallback")
    gateway = _RecordingGateway()
    state = OrchestratorState()
    state.coworkers[cw_state.config.id] = cw_state

    async with _patched_state_and_gateways(
        state=state, gateways={"web": gateway}
    ):
        await m._send_via_coworker(None, chat_id, "from fallback")

    rows = await _query_messages(conv_id)
    assert len(rows) == 1
    assert rows[0]["content"] == "from fallback"
    assert len(gateway.sends) == 1


# ---------------------------------------------------------------------------
# Negative: non-web channels must NOT be persisted by this path
# ---------------------------------------------------------------------------


async def test_telegram_reply_is_not_persisted_to_messages_table() -> None:
    """Telegram chat history lives on Telegram's servers; we deliberately
    don't double-store it in our ``messages`` table. The fix targets the
    web gap only; a regression that over-broadens the persist branch
    would suddenly start logging Telegram replies and inflate the table.
    """
    t = await create_tenant(name="T", slug=f"tg-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="Bob", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    binding = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id, channel_type="telegram",
    )
    chat_id = "telegram-chat-42"
    conv = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id,
        channel_binding_id=binding.id, channel_chat_id=chat_id,
    )

    cw_state = CoworkerState.from_coworker(cw)
    cw_state.channel_bindings["telegram"] = binding
    cw_state.conversations[conv.id] = ConversationState(conversation=conv)

    gateway = _RecordingGateway()
    state = OrchestratorState()
    state.coworkers[cw.id] = cw_state

    async with _patched_state_and_gateways(
        state=state, gateways={"telegram": gateway}
    ):
        await m._send_via_coworker(cw_state, chat_id, "hi from sched")

    rows = await _query_messages(conv.id)
    assert rows == [], (
        "Telegram channel must not be persisted in messages — that's "
        "the responsibility of Telegram's own server-side history"
    )
    # But the gateway publish must still have happened.
    assert len(gateway.sends) == 1


# ---------------------------------------------------------------------------
# Concurrency: repeated calls produce distinct rows (no UUID collision /
# upsert clobber)
# ---------------------------------------------------------------------------


async def test_two_back_to_back_web_replies_produce_two_rows() -> None:
    """``_run_task._on_output`` can fire multiple times per task if the
    agent streams more than one result chunk; each call must land as
    its own row. A regression that reused msg_id (or hashed the text)
    would silently merge them via ``ON CONFLICT ... DO UPDATE`` and
    the user would see only the last one.
    """
    cw_state, chat_id, conv_id = await _seed_web_conv("back-to-back")
    gateway = _RecordingGateway()
    state = OrchestratorState()
    state.coworkers[cw_state.config.id] = cw_state

    async with _patched_state_and_gateways(
        state=state, gateways={"web": gateway}
    ):
        await m._send_via_coworker(cw_state, chat_id, "first")
        await m._send_via_coworker(cw_state, chat_id, "second")

    rows = await _query_messages(conv_id)
    contents = [r["content"] for r in rows]
    assert contents == ["first", "second"], contents
    assert len({r["id"] for r in rows}) == 2, "msg_ids must be unique"
