"""v6.1 §P1.6 — main.py IM 1:1 admission + lazy backfill (T1.7 / T1.8).

The DB + helper layers are tested in tests/auth/ and tests/channels/;
this file drives the full ``_handle_incoming`` code path so the
admission gate, the lazy backfill, and the message-store / enqueue
short-circuit are exercised together.

We stand up real Postgres state (tenant, user, identity, coworker,
binding, conversation) but stub the two non-DB collaborators:
``_gateways`` (so the guidance reply can be observed without
network) and ``_queue`` (so we can confirm the unlinked branch
never enqueues work for the agent).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import rolemesh.main as orchestrator
from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerState,
    OrchestratorState,
)
from rolemesh.db import (
    _get_admin_pool,
    create_channel_binding,
    create_channel_identity,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _build_state(slug_tag: str) -> dict[str, object]:
    """Create the DB rows and the in-memory state the orchestrator
    reads on inbound. Returns the pieces tests need to drive
    ``_handle_incoming`` and assert outcomes.
    """
    t = await create_tenant(
        name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}"
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="Andy",
        folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    binding = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    conv = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id, channel_binding_id=binding.id,
        channel_chat_id="chat-555",
    )
    # OrchestratorState.find_conversation_by_binding_and_chat reads
    # in-memory; we have to mirror what the boot loop would do.
    cw_state = CoworkerState.from_coworker(cw)
    cw_state.channel_bindings[binding.channel_type] = binding
    cw_state.conversations[conv.id] = ConversationState(conversation=conv)
    state = OrchestratorState()
    state.coworkers[cw.id] = cw_state
    return {
        "tenant_id": t.id,
        "user_id": u.id,
        "binding_id": binding.id,
        "chat_id": conv.channel_chat_id,
        "conv": conv,
        "cw_state": cw_state,
        "state": state,
    }


def _patch_orchestrator(
    monkeypatch: pytest.MonkeyPatch, *, state: OrchestratorState
) -> tuple[AsyncMock, SimpleNamespace]:
    """Install stub ``_state`` / ``_gateways`` / ``_queue`` on main.

    The gateway stub records ``send_message`` calls so tests can
    assert on the guidance reply. The queue stub records
    ``enqueue_message_check`` so the unlinked-branch test can
    confirm no agent work is queued.
    """
    send = AsyncMock()
    gateway = SimpleNamespace(send_message=send)
    enqueue = SimpleNamespace(
        enqueue_message_check=lambda *a, **kw: None,
    )
    monkeypatch.setattr(orchestrator, "_state", state)
    monkeypatch.setattr(orchestrator, "_gateways", {"telegram": gateway})
    monkeypatch.setattr(orchestrator, "_queue", enqueue)
    return send, gateway


async def _conv_user_id(conv_id: str) -> str | None:
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM conversations WHERE id = $1::uuid",
            conv_id,
        )
    return str(row["user_id"]) if row and row["user_id"] is not None else None


# ---------------------------------------------------------------------------
# T1.7 — unlinked sender → admission denies, replies guidance, does not store
# ---------------------------------------------------------------------------


async def test_unlinked_sender_admission_denies_and_replies_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = await _build_state("admit-deny")
    send, _gateway = _patch_orchestrator(monkeypatch, state=s["state"])

    enqueue_calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator._queue, "enqueue_message_check",
        lambda *args, **kwargs: enqueue_calls.append((args, kwargs)),
    )

    await orchestrator._handle_incoming(
        binding_id=s["binding_id"],
        chat_id=s["chat_id"],
        sender="stranger-id",
        sender_name="Stranger",
        text="hello",
        timestamp="2026-05-28T00:00:00Z",
        msg_id="msg-1",
        is_group=False,
    )

    # Guidance reply went out to the right binding+chat.
    send.assert_awaited_once()
    args = send.await_args.args
    assert args[0] == s["binding_id"]
    assert args[1] == s["chat_id"]
    assert "RoleMesh" in args[2]
    # Conv stays unbound (lazy backfill did NOT fire).
    assert await _conv_user_id(s["conv"].id) is None
    # No agent work enqueued — the message dies at admission.
    assert enqueue_calls == []


# ---------------------------------------------------------------------------
# T1.8 — linked sender on legacy NULL-user_id conversation → lazy backfill
# ---------------------------------------------------------------------------


async def test_linked_sender_lazy_backfills_conv_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = await _build_state("backfill")
    # Link this sender to the user.
    await create_channel_identity(
        s["tenant_id"], "telegram", "linked-id", s["user_id"]
    )
    send, _gateway = _patch_orchestrator(monkeypatch, state=s["state"])

    enqueue_calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator._queue, "enqueue_message_check",
        lambda *args, **kwargs: enqueue_calls.append((args, kwargs)),
    )

    # Pre-condition: conv has NULL user_id (legacy state).
    assert await _conv_user_id(s["conv"].id) is None
    in_memory_conv = s["cw_state"].conversations[s["conv"].id].conversation
    assert in_memory_conv.user_id is None

    await orchestrator._handle_incoming(
        binding_id=s["binding_id"],
        chat_id=s["chat_id"],
        sender="linked-id",
        sender_name="Linked",
        text="hello",
        timestamp="2026-05-28T00:00:00Z",
        msg_id="msg-1",
        is_group=False,
    )

    # No guidance reply — admission admitted.
    send.assert_not_awaited()
    # DB-side backfill happened.
    assert await _conv_user_id(s["conv"].id) == s["user_id"]
    # In-memory mirror is consistent so downstream paths in the same
    # turn (main.py:782 etc.) read user_id without an extra DB hop.
    assert in_memory_conv.user_id == s["user_id"]
    # Work was enqueued for the agent.
    assert len(enqueue_calls) == 1


async def test_admission_corrects_conv_user_id_after_identity_handover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v6.1 §P1.4 F1 / L2 layer — when a conv carries one
    ``user_id`` but admission resolves the inbound sender to a
    DIFFERENT user_id, admission MUST restamp the conv. This is the
    defense-in-depth complement to the same-transaction NULL inside
    ``delete_channel_identity``: even if a future bug leaves a
    stale stamp on the conv after an unbind/relink, the admission
    layer here re-stamps it to the currently-resolved owner.

    Scenario: User A linked the Telegram chat — conv.user_id=A.
    A unbinds (via some buggy path that did NOT clear the conv).
    User B re-links the same chat — identity row now points to B.
    B's next inbound:
      - admission resolves to B (the identity table is authoritative);
      - lazy-backfill condition must restamp conv.user_id = B;
      - the agent runs B's request under B's identity, not A's.

    Pre-F1 behaviour (``if conv.user_id is None``) would have
    silently left conv.user_id = A and run B's request under A —
    breaking audit attribution.
    """
    s = await _build_state("identity-handover")
    # B is the user that admission will resolve to.
    b = await create_user(
        tenant_id=s["tenant_id"], name="B",
        email=f"b-{uuid.uuid4().hex[:6]}@x.com",
    )
    # The identity row points to B (post-handover state).
    await create_channel_identity(
        s["tenant_id"], "telegram", "linked-id", b.id
    )
    # Conv still carries A's id (the stale-stamp / pre-F1 state).
    user_a_id = s["user_id"]
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET user_id = $1::uuid WHERE id = $2::uuid",
            user_a_id, s["conv"].id,
        )
    s["cw_state"].conversations[s["conv"].id].conversation.user_id = user_a_id

    _send, _gateway = _patch_orchestrator(monkeypatch, state=s["state"])
    enqueue_calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator._queue, "enqueue_message_check",
        lambda *args, **kwargs: enqueue_calls.append((args, kwargs)),
    )

    await orchestrator._handle_incoming(
        binding_id=s["binding_id"],
        chat_id=s["chat_id"],
        sender="linked-id",
        sender_name="B-now",
        text="hello",
        timestamp="2026-05-28T00:00:00Z",
        msg_id="msg-1",
        is_group=False,
    )

    # Conv was restamped to B in both DB and in-memory.
    assert await _conv_user_id(s["conv"].id) == b.id, (
        "admission must restamp conv.user_id to the currently-resolved "
        "user; leaving A would run B's request under A's identity"
    )
    in_memory = s["cw_state"].conversations[s["conv"].id].conversation
    assert in_memory.user_id == b.id
    # And the message goes through (admission passed, message enqueued).
    assert len(enqueue_calls) == 1


async def test_lazy_backfill_fills_null_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original backfill case stays covered: a conv with
    ``user_id IS NULL`` (legacy / web-created pre-link) gets stamped
    on the first admitted inbound. This is the same code path as the
    F1 correction; pin both branches so a future "optimise out the
    UPDATE when conv.user_id is None" mutation cannot silently
    regress.
    """
    s = await _build_state("backfill-null")
    await create_channel_identity(
        s["tenant_id"], "telegram", "linked-id", s["user_id"]
    )
    # conv.user_id starts NULL by default (see _build_state). Sanity:
    assert await _conv_user_id(s["conv"].id) is None
    _send, _gateway = _patch_orchestrator(monkeypatch, state=s["state"])
    monkeypatch.setattr(
        orchestrator._queue, "enqueue_message_check",
        lambda *args, **kwargs: None,
    )

    await orchestrator._handle_incoming(
        binding_id=s["binding_id"],
        chat_id=s["chat_id"],
        sender="linked-id",
        sender_name="Linked",
        text="hello",
        timestamp="2026-05-28T00:00:00Z",
        msg_id="msg-1",
        is_group=False,
    )
    assert await _conv_user_id(s["conv"].id) == s["user_id"]
