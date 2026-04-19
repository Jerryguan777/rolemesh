"""Engine integration tests — real Postgres, fake NATS + channel.

Focus on the transitions and audit shape that the spec pins:
  - proposal with match → pending + notify approvers, audit: created
  - proposal with NO match → executed-path + audit: created, no skipped
  - proposal with empty approvers → skipped + audit: created, skipped
  - auto_intercept dedup: second hit with same hash is dropped
  - auto_intercept drops if policy disabled between hook and orchestrator
  - decision approve → publishes approval.decided + audit: approved
  - decision reject → notifies origin + audit: rejected, no publish
  - concurrent decide → second call raises ConflictError (409 surface)
  - non-approver decide → ForbiddenError (403 surface)
  - cancel_for_job → only pending cancelled; approved survives
  - expire_stale_requests → pending past expires_at becomes expired
  - reconcile_stuck_requests → republish on approved-for-60s,
    transition executing-for-5min to execution_stale

Actor_user_id rules baked into assertions — auto_intercept created is
NULL, proposal created is user_id, approved/rejected is the approver,
system transitions (expired/cancelled/skipped/executed) are NULL.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rolemesh.approval.engine import (
    ApprovalEngine,
    ConflictError,
    ForbiddenError,
)
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publishes.append((subject, data))


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_to_conversation(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))


def _resolver(
    *,
    webui_base_url: str | None = None,
) -> NotificationTargetResolver:
    async def _no_convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    async def _get_conv(conv_id: str) -> object | None:
        # Always say it exists for these tests; engine's correctness
        # does not depend on conv lookup for the origin fallback.
        return object()

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_no_convs,
        get_conversation=_get_conv,
        webui_base_url=webui_base_url,
    )


async def _seed(
    *,
    with_policy: bool = True,
    approver_ids: list[str] | None = None,
) -> tuple[str, str, str, str, str, str | None]:
    """Create tenant/user/coworker/binding/conversation and an optional policy."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="Alice", email="a@x.com", role="owner"
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    policy_id: str | None = None
    if with_policy:
        p = await create_approval_policy(
            tenant_id=t.id,
            coworker_id=cw.id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"field": "amount", "op": ">", "value": 1000},
            approver_user_ids=approver_ids if approver_ids is not None else [u.id],
            priority=0,
        )
        policy_id = p.id
    return t.id, u.id, cw.id, conv.id, "job-1", policy_id


def _engine(
    publisher: _FakePublisher | None = None,
    channel: _FakeChannel | None = None,
) -> tuple[ApprovalEngine, _FakePublisher, _FakeChannel]:
    pub = publisher or _FakePublisher()
    ch = channel or _FakeChannel()
    return (
        ApprovalEngine(publisher=pub, channel_sender=ch, resolver=_resolver()),
        pub,
        ch,
    )


async def _call_proposal(engine: ApprovalEngine, payload: dict[str, Any]) -> None:
    """Wrapper that mirrors the IPC-dispatcher contract: pass the
    orchestrator-trusted tenant_id / coworker_id alongside the payload.
    Tests use the values claimed inside the payload itself (there is no
    mismatch scenario to exercise here)."""
    await engine.handle_proposal(
        payload,
        tenant_id=str(payload.get("tenantId", "")),
        coworker_id=str(payload.get("coworkerId", "")),
    )


async def _call_auto_intercept(
    engine: ApprovalEngine, payload: dict[str, Any]
) -> None:
    await engine.handle_auto_intercept(
        payload,
        tenant_id=str(payload.get("tenantId", "")),
        coworker_id=str(payload.get("coworkerId", "")),
    )


# ---------------------------------------------------------------------------
# submit_proposal path
# ---------------------------------------------------------------------------


class TestTrustedTenantGuard:
    """The engine must refuse messages whose payload's tenantId / coworkerId
    disagrees with the orchestrator's trusted lookup (container-compromise
    defense). Tested at the engine boundary since the IPC dispatcher's
    consistency check and the engine's own check are the two places that
    enforce the invariant."""

    async def test_mismatched_tenant_is_dropped(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, pub, ch = _engine()
        # Payload claims a DIFFERENT tenant_id than the trusted one we
        # will pass alongside. The engine must drop silently (no row
        # created, no publish, no notification).
        forged_tenant = str(uuid.uuid4())
        await engine.handle_proposal(
            {
                "tenantId": forged_tenant,  # forged by caller
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "r",
                "actions": [
                    {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 5000}}
                ],
            },
            tenant_id=tenant_id,  # trusted
            coworker_id=cw_id,
        )
        assert await list_approval_requests(tenant_id) == []
        assert pub.publishes == []
        assert ch.sent == []

    async def test_mismatched_coworker_is_dropped(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, pub, ch = _engine()
        forged_cw = str(uuid.uuid4())
        await engine.handle_proposal(
            {
                "tenantId": tenant_id,
                "coworkerId": forged_cw,  # forged by caller
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "r",
                "actions": [
                    {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 5000}}
                ],
            },
            tenant_id=tenant_id,
            coworker_id=cw_id,  # trusted
        )
        assert await list_approval_requests(tenant_id) == []
        assert pub.publishes == []
        assert ch.sent == []


class TestStrictestPolicyWins:
    async def test_batch_picks_strictest_by_priority(self) -> None:
        # Three actions; two match a low-priority policy, one matches a
        # high-priority one. Engine should use the high-priority policy's
        # approvers + expiry for the request as a whole.
        tenant_id, user_id, cw_id, conv_id, job_id, _ = await _seed(
            with_policy=False
        )
        manager = await create_user(
            tenant_id=tenant_id, name="Manager", email="m@x.com", role="admin"
        )
        low = await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"always": True},
            approver_user_ids=[user_id],
            priority=1,
        )
        high = await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="erp",
            tool_name="cancel_order",
            condition_expr={"always": True},
            approver_user_ids=[manager.id],
            priority=10,
        )
        engine, _pub, _ch = _engine()
        await _call_proposal(
            engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "batch",
                "actions": [
                    {"mcp_server": "erp", "tool_name": "refund", "params": {}},
                    {"mcp_server": "erp", "tool_name": "refund", "params": {"id": 2}},
                    {"mcp_server": "erp", "tool_name": "cancel_order", "params": {}},
                ],
            },
        )
        reqs = await list_approval_requests(tenant_id)
        assert len(reqs) == 1
        req = reqs[0]
        assert req.status == "pending"
        # Strictest wins: high-priority policy's approvers are snapshot.
        assert req.resolved_approvers == [manager.id]
        assert req.policy_id == high.id
        # low is still around, just not selected
        assert low.id != req.policy_id


class TestHandleProposal:
    async def test_matching_proposal_creates_pending_and_notifies(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, pub, ch = _engine()

        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "refund for customer 42",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 5000},
                    }
                ],
            }
        )

        reqs = await list_approval_requests(tenant_id)
        assert len(reqs) == 1
        assert reqs[0].status == "pending"
        audit = await list_approval_audit(reqs[0].id)
        assert [e.action for e in audit] == ["created"]
        assert audit[0].actor_user_id == user_id, (
            "proposal 'created' audit must record the originating user"
        )
        assert pub.publishes == [], "pending request must not publish approval.decided yet"
        assert ch.sent, "at least one approver notification must be attempted"

    async def test_no_match_path_creates_executed_trail(self) -> None:
        # Proposal that does not match any policy. Engine still creates
        # a request (for audit) and publishes approval.decided so the
        # Worker executes the actions.
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, pub, _ch = _engine()

        # Action that does not match the amount>1000 policy.
        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "tiny refund",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 1},
                    }
                ],
            }
        )

        reqs = await list_approval_requests(tenant_id)
        assert len(reqs) == 1
        assert reqs[0].status == "approved", (
            "no-match proposal short-circuits to approved for Worker pickup"
        )
        # The audit trail for the no-match path MUST include the system
        # 'approved' row so it matches the 4-row shape of a normal approve
        # flow (created, approved, executing, executed). Without this
        # the auto-executed path diverges from the trail shape admins
        # would expect to reconstruct from audit_log alone.
        audit = await list_approval_audit(reqs[0].id)
        assert [e.action for e in audit] == ["created", "approved"]
        assert audit[0].actor_user_id == user_id
        assert audit[1].actor_user_id is None, (
            "system transition to 'approved' should have NULL actor"
        )
        # decided event published for Worker
        assert len(pub.publishes) == 1
        assert pub.publishes[0][0] == f"approval.decided.{reqs[0].id}"

    async def test_empty_approvers_triggers_skipped(self) -> None:
        # Policy with no explicit approvers AND no user-agent assignment
        # AND no owner fallback (we pass approver_ids=[] but also delete
        # the owner by making the user a member).
        t = await create_tenant(name="Tnone", slug=f"tn-{uuid.uuid4().hex[:8]}")
        member = await create_user(
            tenant_id=t.id, name="Member", email="m@x.com", role="member"
        )
        cw = await create_coworker(
            tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
        )
        b = await create_channel_binding(
            coworker_id=cw.id,
            tenant_id=t.id,
            channel_type="telegram",
            credentials={"bot_token": "x"},
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
            condition_expr={"always": True},
            approver_user_ids=[],
        )

        engine, _pub, ch = _engine()
        await _call_proposal(engine,
            {
                "tenantId": t.id,
                "coworkerId": cw.id,
                "conversationId": conv.id,
                "jobId": "job-skip",
                "userId": member.id,
                "rationale": "r",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 100},
                    }
                ],
            }
        )
        reqs = await list_approval_requests(t.id)
        assert len(reqs) == 1
        assert reqs[0].status == "skipped"
        audit = await list_approval_audit(reqs[0].id)
        assert [e.action for e in audit] == ["created", "skipped"]
        # Originating conversation was notified of the skip.
        assert any(conv.id == c for c, _t in ch.sent)


# ---------------------------------------------------------------------------
# auto_intercept path
# ---------------------------------------------------------------------------


class TestAutoIntercept:
    async def test_second_identical_intercept_is_deduped(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, _pub, _ch = _engine()

        payload: dict[str, Any] = {
            "tenantId": tenant_id,
            "coworkerId": cw_id,
            "conversationId": conv_id,
            "jobId": job_id,
            "userId": user_id,
            "mcp_server_name": "erp",
            "tool_name": "refund",
            "tool_params": {"amount": 5000, "order_id": "o1"},
            "action_hash": "deduped-hash",
        }
        await _call_auto_intercept(engine, payload)
        await _call_auto_intercept(engine, payload)

        reqs = await list_approval_requests(tenant_id)
        assert len(reqs) == 1, "dedup must prevent a second pending request"

    async def test_intercept_drops_when_policy_disabled_server_side(self) -> None:
        # Hook saw a stale snapshot; orchestrator's live query no longer
        # matches, so no request is created.
        t = await create_tenant(name="Tx", slug=f"tx-{uuid.uuid4().hex[:8]}")
        u = await create_user(
            tenant_id=t.id, name="Alice", email="a@x.com", role="owner"
        )
        cw = await create_coworker(
            tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
        )
        b = await create_channel_binding(
            coworker_id=cw.id,
            tenant_id=t.id,
            channel_type="telegram",
            credentials={"bot_token": "x"},
        )
        conv = await create_conversation(
            tenant_id=t.id,
            coworker_id=cw.id,
            channel_binding_id=b.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        # No enabled policy matches the call (we intentionally create
        # a disabled one to simulate the hook's stale snapshot).
        await create_approval_policy(
            tenant_id=t.id,
            coworker_id=cw.id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"always": True},
            approver_user_ids=[u.id],
            enabled=False,
        )
        engine, _pub, _ch = _engine()
        await _call_auto_intercept(engine,
            {
                "tenantId": t.id,
                "coworkerId": cw.id,
                "conversationId": conv.id,
                "jobId": "j1",
                "userId": u.id,
                "mcp_server_name": "erp",
                "tool_name": "refund",
                "tool_params": {"amount": 9999},
                "action_hash": "does-not-matter",
            }
        )
        assert await list_approval_requests(t.id) == []

    async def test_intercept_created_audit_has_null_actor(self) -> None:
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        engine, _pub, _ch = _engine()
        await _call_auto_intercept(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": "j2",
                "userId": user_id,
                "mcp_server_name": "erp",
                "tool_name": "refund",
                "tool_params": {"amount": 5000},
                "action_hash": "h-auto-1",
            }
        )
        reqs = await list_approval_requests(tenant_id)
        audit = await list_approval_audit(reqs[0].id)
        assert audit[0].action == "created"
        assert audit[0].actor_user_id is None


# ---------------------------------------------------------------------------
# Decision path
# ---------------------------------------------------------------------------


class TestDecision:
    async def test_approve_writes_audit_and_publishes(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, pub, _ch = _engine()
        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "r",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 5000},
                    }
                ],
            }
        )
        req = (await list_approval_requests(tenant_id))[0]

        updated = await engine.handle_decision(
            request_id=req.id, action="approve", user_id=user_id
        )
        assert updated.status == "approved"
        assert any(
            s == f"approval.decided.{req.id}" for s, _d in pub.publishes
        )
        audit = await list_approval_audit(req.id)
        assert [e.action for e in audit] == ["created", "approved"]
        assert audit[1].actor_user_id == user_id

    async def test_reject_publishes_decided_event_with_status_rejected(self) -> None:
        # Decision events flow through NATS regardless of outcome — the
        # orchestrator-side Worker dispatches the rejection notification
        # to keep the WebUI's REST handler free of gateway dependencies.
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, pub, _ch = _engine()
        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "r",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 5000},
                    }
                ],
            }
        )
        req = (await list_approval_requests(tenant_id))[0]

        await engine.handle_decision(
            request_id=req.id,
            action="reject",
            user_id=user_id,
            note="not this quarter",
        )
        matching = [
            (s, d) for s, d in pub.publishes if s == f"approval.decided.{req.id}"
        ]
        assert len(matching) == 1
        payload = json.loads(matching[0][1].decode())
        assert payload["status"] == "rejected"
        assert payload["note"] == "not this quarter"

    async def test_concurrent_decide_raises_conflict(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, _pub, _ch = _engine()
        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "r",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 5000},
                    }
                ],
            }
        )
        req = (await list_approval_requests(tenant_id))[0]
        await engine.handle_decision(
            request_id=req.id, action="approve", user_id=user_id
        )
        with pytest.raises(ConflictError):
            await engine.handle_decision(
                request_id=req.id, action="reject", user_id=user_id
            )

    async def test_non_approver_decide_raises_forbidden(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        outsider = await create_user(
            tenant_id=tenant_id, name="Eve", email="e@x.com", role="member"
        )
        engine, _pub, _ch = _engine()
        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "r",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 5000},
                    }
                ],
            }
        )
        req = (await list_approval_requests(tenant_id))[0]
        with pytest.raises(ForbiddenError):
            await engine.handle_decision(
                request_id=req.id, action="approve", user_id=outsider.id
            )


# ---------------------------------------------------------------------------
# cancel_for_job + maintenance loops
# ---------------------------------------------------------------------------


class TestCancelAndMaintenance:
    async def test_cancel_preserves_approved_rows(self) -> None:
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        engine, _pub, _ch = _engine()
        # Create two proposals in the same job — one we approve first.
        for _ in range(2):
            await _call_proposal(engine,
                {
                    "tenantId": tenant_id,
                    "coworkerId": cw_id,
                    "conversationId": conv_id,
                    "jobId": job_id,
                    "userId": user_id,
                    "rationale": "r",
                    "actions": [
                        {
                            "mcp_server": "erp",
                            "tool_name": "refund",
                            "params": {"amount": 5000 + _},
                        }
                    ],
                }
            )
        reqs = await list_approval_requests(tenant_id)
        # Approve one.
        first_id = reqs[0].id
        await engine.handle_decision(
            request_id=first_id, action="approve", user_id=user_id
        )
        # Cancel the job.
        await engine.cancel_for_job(job_id)
        after = {r.id: r.status for r in await list_approval_requests(tenant_id)}
        assert after[first_id] == "approved"
        # The other (pending) must be cancelled.
        other = next(rid for rid in after if rid != first_id)
        assert after[other] == "cancelled"

    async def test_expire_moves_pending_past_deadline(self) -> None:
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        # Build a request directly in the past.
        from rolemesh.db.pg import create_approval_request, list_approval_policies

        pol = (await list_approval_policies(tenant_id))[0]
        req = await create_approval_request(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            conversation_id=conv_id,
            policy_id=pol.id,
            user_id=user_id,
            job_id="past",
            mcp_server_name="erp",
            actions=[
                {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 9999}}
            ],
            action_hashes=["h"],
            rationale=None,
            source="proposal",
            status="pending",
            resolved_approvers=[user_id],
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        engine, _pub, _ch = _engine()
        count = await engine.expire_stale_requests()
        assert count >= 1
        after = await get_approval_request(req.id)
        assert after is not None and after.status == "expired"
        audit = await list_approval_audit(req.id)
        assert any(e.action == "expired" and e.actor_user_id is None for e in audit)

    async def test_reconcile_republishes_stuck_approved(self) -> None:
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        from rolemesh.db.pg import create_approval_request, list_approval_policies

        pol = (await list_approval_policies(tenant_id))[0]
        req = await create_approval_request(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            conversation_id=conv_id,
            policy_id=pol.id,
            user_id=user_id,
            job_id="stuck",
            mcp_server_name="erp",
            actions=[{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
            action_hashes=["h"],
            rationale=None,
            source="proposal",
            status="approved",
            resolved_approvers=[user_id],
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
        )
        engine, pub, _ch = _engine()
        # With older_than_seconds=60 the row (just-inserted) would not
        # appear stuck. To keep the test deterministic AND exercise the
        # engine's own 60s threshold, we force the updated_at into the
        # past via raw SQL.
        from rolemesh.db.pg import _get_pool

        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE approval_requests SET updated_at = now() - interval '2 minutes' WHERE id = $1::uuid",
                req.id,
            )
        result = await engine.reconcile_stuck_requests()
        assert result["republished"] >= 1
        assert any(
            s == f"approval.decided.{req.id}" for s, _d in pub.publishes
        )

    async def test_reconcile_marks_stuck_executing_stale(self) -> None:
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        from rolemesh.db.pg import (
            _get_pool,
            create_approval_request,
            list_approval_policies,
        )

        pol = (await list_approval_policies(tenant_id))[0]
        req = await create_approval_request(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            conversation_id=conv_id,
            policy_id=pol.id,
            user_id=user_id,
            job_id="stale",
            mcp_server_name="erp",
            actions=[{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
            action_hashes=["h"],
            rationale=None,
            source="proposal",
            status="executing",
            resolved_approvers=[user_id],
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE approval_requests SET updated_at = now() - interval '10 minutes' WHERE id = $1::uuid",
                req.id,
            )
        engine, _pub, _ch = _engine()
        await engine.reconcile_stuck_requests()
        after = await get_approval_request(req.id)
        assert after is not None and after.status == "execution_stale"
        audit = await list_approval_audit(req.id)
        assert any(e.action == "execution_stale" for e in audit)


# Silence ruff / unused-import warnings in the shared asyncio import kept
# around for future tests.
_ = (asyncio, json)
