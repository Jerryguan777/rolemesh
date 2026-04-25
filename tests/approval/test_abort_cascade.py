"""End-to-end test of the Stop-button → approval cancel cascade.

Flow under test (mirrors docs/backend-stop-contract.md §8):
  1. An agent run publishes an auto_approval_request which the engine
     turns into a pending row for job_id=X.
  2. User clicks Stop. Agent runner publishes approval.cancel_for_job.X.
  3. Engine's cancel_for_job moves pending→cancelled, writes an audit
     row, and notifies the originating conversation.

We drive the engine directly (same harness as test_engine.py) and
assert on the DB shape — NATS wiring is already covered by the main.py
integration and pinning it here would require live NATS.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from rolemesh.approval.engine import ApprovalEngine
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.db.pg import (
    create_approval_policy,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    get_approval_request,
    list_approval_audit,
    list_approval_requests,
)

pytestmark = pytest.mark.usefixtures("test_db")


class _FakePublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.publishes.append((subject, data))


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_to_conversation(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))


def _resolver() -> NotificationTargetResolver:
    async def _convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    async def _conv(conv_id: str) -> object | None:
        return object()

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_conv,
        webui_base_url=None,
    )


async def _call_proposal(
    engine: ApprovalEngine, payload: dict[str, Any]
) -> None:
    """Mirror the IPC dispatcher: pass trusted tenant_id / coworker_id
    alongside the payload. Tests take the values claimed inside the
    payload since no mismatch scenario is being exercised here."""
    await engine.handle_proposal(
        payload,
        tenant_id=str(payload.get("tenantId", "")),
        coworker_id=str(payload.get("coworkerId", "")),
    )


async def _seed_two_requests(job_id: str) -> tuple[str, str, str, str, str, str]:
    """Seed (tenant, owner, coworker, conv, req_pending_id, req_approved_id)."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(tenant_id=t.id, name="O", email="o@x.com", role="owner")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    c = await create_conversation(
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
        condition_expr={"always": True},
        approver_user_ids=[u.id],
    )
    engine = ApprovalEngine(
        publisher=_FakePublisher(),
        channel_sender=_FakeChannel(),
        resolver=_resolver(),
    )
    # Pending request in this job
    await _call_proposal(engine,
        {
            "tenantId": t.id,
            "coworkerId": cw.id,
            "conversationId": c.id,
            "jobId": job_id,
            "userId": u.id,
            "rationale": "r",
            "actions": [
                {"mcp_server": "erp", "tool_name": "refund", "params": {"i": 1}}
            ],
        }
    )
    # Second request in same job → approve it before the cancel fires.
    await _call_proposal(engine,
        {
            "tenantId": t.id,
            "coworkerId": cw.id,
            "conversationId": c.id,
            "jobId": job_id,
            "userId": u.id,
            "rationale": "r",
            "actions": [
                {"mcp_server": "erp", "tool_name": "refund", "params": {"i": 2}}
            ],
        }
    )
    reqs = sorted(
        await list_approval_requests(t.id),
        key=lambda r: r.actions[0]["params"]["i"],
    )
    await engine.handle_decision(
        request_id=reqs[1].id, tenant_id=t.id, action="approve", user_id=u.id
    )
    return t.id, u.id, cw.id, c.id, reqs[0].id, reqs[1].id


async def test_stop_cascade_cancels_pending_preserves_approved() -> None:
    job_id = "job-stop-1"
    tenant_id, _user_id, _cw, conv_id, pending_id, approved_id = await _seed_two_requests(job_id)

    pub = _FakePublisher()
    ch = _FakeChannel()
    engine = ApprovalEngine(
        publisher=pub, channel_sender=ch, resolver=_resolver()
    )
    cancelled = await engine.cancel_for_job(job_id)

    assert pending_id in cancelled
    assert approved_id not in cancelled

    pending_after = await get_approval_request(pending_id, tenant_id=tenant_id)
    approved_after = await get_approval_request(approved_id, tenant_id=tenant_id)
    assert pending_after is not None and pending_after.status == "cancelled"
    assert approved_after is not None and approved_after.status == "approved"

    # Audit trail for the cancelled row includes a cancelled entry with
    # a NULL actor (system transition).
    audit = await list_approval_audit(pending_id, tenant_id=tenant_id)
    cancelled_entries = [e for e in audit if e.action == "cancelled"]
    assert len(cancelled_entries) == 1
    assert cancelled_entries[0].actor_user_id is None

    # Origin conversation got a cancellation notification.
    assert any(conv_id == c and "cancelled" in t for c, t in ch.sent)


async def test_cancel_noop_when_no_pending_rows_for_job() -> None:
    engine = ApprovalEngine(
        publisher=_FakePublisher(),
        channel_sender=_FakeChannel(),
        resolver=_resolver(),
    )
    result = await engine.cancel_for_job("job-does-not-exist")
    assert result == []
