"""v6.1 §P2.8 — interactive-turn entry guard (T2a.14).

When a user has an approval pending in a conversation, a fresh
inbound message must not dispatch the agent. The orchestrator
should:

  - send the canonical PENDING_TURN_GUIDE_TEXT via the gateway
  - leave ``conv_state.last_agent_timestamp`` untouched so the
    queued messages reprocess on the next tick after the
    decision lands
  - return ``True`` (queue is idle)

The guard is only at the interactive turn entry — per-tool-call
gating is already covered by the hook fail-close, so this file
deliberately does not assert anything about the hook path.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import rolemesh.main as orchestrator
from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerState,
    OrchestratorState,
)
from rolemesh.db import (
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    store_message,
)
from rolemesh.approval.notification import PENDING_TURN_GUIDE_TEXT

pytestmark = pytest.mark.usefixtures("test_db")


async def _build_state(slug_tag: str) -> dict[str, Any]:
    """Real DB rows + the in-memory state main.py reads."""
    t = await create_tenant(
        name="T", slug=f"{slug_tag}-{uuid.uuid4().hex[:6]}"
    )
    u = await create_user(
        tenant_id=t.id, name="U",
        email=f"u-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
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
        channel_chat_id=f"chat-{uuid.uuid4().hex[:6]}",
        # 1:1 by construction — requires_trigger=False mirrors the
        # Telegram-1on1 auto-create path so the trigger check does
        # not pre-empt the pending-approval guard under test.
        requires_trigger=False,
    )
    cw_state = CoworkerState.from_coworker(cw)
    cw_state.channel_bindings[binding.channel_type] = binding
    cw_state.conversations[conv.id] = ConversationState(conversation=conv)
    state = OrchestratorState()
    state.coworkers[cw.id] = cw_state
    return {
        "tenant_id": t.id,
        "user_id": u.id,
        "coworker_id": cw.id,
        "binding_id": binding.id,
        "chat_id": conv.channel_chat_id,
        "conv": conv,
        "cw_state": cw_state,
        "conv_state": cw_state.conversations[conv.id],
        "state": state,
    }


def _patch_orchestrator(
    monkeypatch: pytest.MonkeyPatch, *, state: OrchestratorState
) -> tuple[AsyncMock, AsyncMock]:
    """Stub gateway + executor + queue."""
    from rolemesh.agent.executor import AgentOutput

    send = AsyncMock()
    set_typing = AsyncMock()
    gateway = SimpleNamespace(send_message=send, set_typing=set_typing)
    # Executor returns a real AgentOutput so the caller's accesses
    # (output.new_session_id, output.status, etc.) don't recursively
    # produce AsyncMock objects and explode at SQL bind time.
    execute_mock = AsyncMock(
        return_value=AgentOutput(status="success", result="ok", new_session_id=None),
    )
    executor = SimpleNamespace(execute=execute_mock)
    monkeypatch.setattr(orchestrator, "_state", state)
    monkeypatch.setattr(orchestrator, "_gateways", {"telegram": gateway})
    monkeypatch.setattr(
        orchestrator,
        "_executors",
        {"claude": executor, "claude_code": executor, "claude_sdk": executor},
    )
    monkeypatch.setattr(orchestrator, "_executor", executor)
    monkeypatch.setattr(
        orchestrator, "_queue",
        SimpleNamespace(
            enqueue_message_check=lambda *a, **kw: None,  # noqa: ARG005
            register_process=lambda *a, **kw: None,  # noqa: ARG005
            request_shutdown=lambda *a, **kw: None,  # noqa: ARG005
            set_process_messages_fn=lambda *a, **kw: None,  # noqa: ARG005
        ),
    )
    return send, execute_mock


async def _insert_message(
    *, tenant_id: str, conv_id: str, content: str, ts: str
) -> None:
    await store_message(
        tenant_id=tenant_id,
        conversation_id=conv_id,
        msg_id=f"msg-{uuid.uuid4().hex[:6]}",
        sender="user",
        sender_name="U",
        content=content,
        timestamp=ts,
    )


async def _insert_pending_approval(
    s: dict[str, Any],
) -> str:
    """Insert a policy + pending approval_request bound to s['conv']."""
    p = await create_approval_policy(
        tenant_id=s["tenant_id"],
        coworker_id=s["coworker_id"],
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[s["user_id"]],
    )
    req = await create_approval_request(
        tenant_id=s["tenant_id"],
        coworker_id=s["coworker_id"],
        conversation_id=s["conv"].id,
        policy_id=p.id,
        user_id=s["user_id"],
        job_id="job-pending",
        mcp_server_name="erp",
        actions=[{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
        action_hashes=["h"],
        rationale=None,
        source="proposal",
        status="pending",
        resolved_approvers=[s["user_id"]],
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    return req.id


async def test_pending_approval_blocks_turn_and_sends_guide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2a.14 — with a pending row, the agent must not run and the
    canonical guide text must be sent via the gateway exactly once."""
    s = await _build_state("pending-guard")
    send, execute = _patch_orchestrator(monkeypatch, state=s["state"])
    await _insert_pending_approval(s)
    # New inbound message arrives — would normally trigger an agent run.
    ts = "2026-05-28T01:00:00Z"
    await _insert_message(
        tenant_id=s["tenant_id"], conv_id=s["conv"].id,
        content="hi, did the refund go through?", ts=ts,
    )

    rv = await orchestrator._process_conversation_messages(s["conv"].id)
    assert rv is True

    # Guide text was sent via the gateway — exact string match so a
    # silent translation drift trips the test.
    send.assert_awaited_once()
    args = send.await_args.args
    assert args[0] == s["binding_id"]
    assert args[1] == s["chat_id"]
    assert args[2] == PENDING_TURN_GUIDE_TEXT

    # Agent executor was NOT called — the gate fired before dispatch.
    execute.assert_not_awaited()

    # The cursor stays put so the queued message re-processes after
    # the user decides. Verifying the conv_state field directly is
    # the cleanest way to assert this; advancing it here would
    # silently swallow the user's question.
    assert s["conv_state"].last_agent_timestamp == ""


async def test_no_pending_lets_turn_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation guard — without any pending row, the gate must not
    fire. If a refactor inverts the boolean by mistake, this test
    fails together with the positive case above.
    """
    s = await _build_state("no-pending")
    send, execute = _patch_orchestrator(monkeypatch, state=s["state"])
    # Insert a message but no pending approval.
    await _insert_message(
        tenant_id=s["tenant_id"], conv_id=s["conv"].id,
        content="hi", ts="2026-05-28T01:00:00Z",
    )

    await orchestrator._process_conversation_messages(s["conv"].id)

    # No guide text sent.
    send.assert_not_awaited()
    # The executor was reached (we don't care about the agent output —
    # just that the gate didn't pre-empt).
    execute.assert_awaited()


async def test_resolved_approval_does_not_block_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An *approved* (or rejected / expired / etc.) row must not gate
    the next turn. Without this assertion a mutation that flipped
    ``status = 'pending'`` to ``status != 'pending'`` would still
    pass the positive test but break here.
    """
    s = await _build_state("resolved-no-gate")
    send, execute = _patch_orchestrator(monkeypatch, state=s["state"])
    req_id = await _insert_pending_approval(s)
    # Transition to approved — the row is no longer pending.
    from rolemesh.db import set_approval_status
    await set_approval_status(req_id, "approved", tenant_id=s["tenant_id"])

    await _insert_message(
        tenant_id=s["tenant_id"], conv_id=s["conv"].id,
        content="continue please", ts="2026-05-28T01:00:00Z",
    )

    await orchestrator._process_conversation_messages(s["conv"].id)

    send.assert_not_awaited()
    execute.assert_awaited()
