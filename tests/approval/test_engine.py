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
from rolemesh.approval.notification import (
    ApprovalCardPayload,
    NotificationTargetResolver,
)
from rolemesh.db import (
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


class _CardCapableFakeChannel(_FakeChannel):
    """A channel-sender spy that also opts into the card surface.

    v6.1 §P2.7 / §P2b.1 — the dispatcher in ``notification.py`` picks
    the card path when ``send_approval_card`` exists; this spy lets
    the engine tests assert the orchestrator actually went down that
    path instead of silently falling back to text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[tuple[str, ApprovalCardPayload]] = []

    async def send_approval_card(
        self, conversation_id: str, card: ApprovalCardPayload
    ) -> None:
        self.cards.append((conversation_id, card))


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
        # high-priority one. Engine should select the high-priority
        # policy for ``policy_id`` + expiry. Under v6.1 self-approval,
        # the policy's ``approver_user_ids`` field is intentionally
        # ignored — ``resolved_approvers`` snapshots the requester
        # (decision #1).
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
            # The policy still carries a non-requester approver (DB
            # column survives as SoD seam), but the v6.1 engine must
            # not honour it — proven by the assertion below.
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
        # Strictest wins for policy selection (drives expiry,
        # post_exec_mode, etc.). high.priority > low.priority.
        assert req.policy_id == high.id
        # low is still around, just not selected.
        assert low.id != req.policy_id
        # v6.1 self-approval: the requester is the sole resolved
        # approver, regardless of policy.approver_user_ids. If a
        # future refactor reintroduces three-tier fallback, the
        # observed value would flip back to [manager.id] and this
        # assertion would catch it.
        assert req.resolved_approvers == [user_id], (
            "v6.1 self-approval: resolved_approvers must be the "
            "requester, not the policy's approver_user_ids"
        )


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
        audit = await list_approval_audit(reqs[0].id, tenant_id=reqs[0].tenant_id)
        assert [e.action for e in audit] == ["created"]
        assert audit[0].actor_user_id == user_id, (
            "proposal 'created' audit must record the originating user"
        )
        decided = [
            (s, d) for s, d in pub.publishes if s.startswith("approval.decided.")
        ]
        assert decided == [], (
            "pending request must not publish approval.decided yet"
        )
        # 03a PR2: the engine *does* publish web.approval.required for
        # the SPA forwarder; assert the subject shape but don't assert
        # the full envelope here (the per-engine integration tests
        # exercise the body).
        webreq = [
            (s, d) for s, d in pub.publishes if s.startswith("web.approval.required.")
        ]
        assert len(webreq) == 1
        assert webreq[0][0] == f"web.approval.required.{conv_id}"
        assert ch.sent, "at least one approver notification must be attempted"

    async def test_notify_approvers_routes_card_capable_channels_through_dispatcher(
        self,
    ) -> None:
        """v6.1 §P2b.1 / §P2.7 — ``_notify_approvers`` must build an
        :class:`ApprovalCardPayload` and call
        :func:`deliver_approval_card_or_text` so card-capable channels
        (Telegram, Web) receive the structured payload. The Phase 2a
        wiring added the dispatcher helper but the engine's call site
        was still ``send_to_conversation`` — closing that gap is the
        whole point of §P2b.1 step 3. A future regression that
        reverts the call site to plain text would silently break
        Telegram inline buttons without any other test catching it.
        """
        tenant_id, user_id, cw_id, conv_id, job_id, _p = await _seed()
        spy = _CardCapableFakeChannel()
        engine, _pub, ch = _engine(channel=spy)
        assert ch is spy

        await _call_proposal(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": job_id,
                "userId": user_id,
                "rationale": "refund for customer 99",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 5000},
                    }
                ],
            }
        )

        assert spy.cards, (
            "card-capable channel must receive an ApprovalCardPayload — "
            "engine still falls back to plain send_to_conversation"
        )
        assert spy.sent == [], (
            "card-capable channel must NOT also be sent text — that "
            "would double-notify the approver"
        )
        delivered_conv, payload = spy.cards[0]
        assert delivered_conv == conv_id
        # The dispatcher contract: text_fallback for Slack-style
        # channels, but the request_id MUST be carried so Telegram
        # can build callback_data without re-querying the engine.
        reqs = await list_approval_requests(tenant_id)
        assert payload.request_id == reqs[0].id

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
        audit = await list_approval_audit(reqs[0].id, tenant_id=reqs[0].tenant_id)
        assert [e.action for e in audit] == ["created", "approved"]
        assert audit[0].actor_user_id == user_id
        assert audit[1].actor_user_id is None, (
            "system transition to 'approved' should have NULL actor"
        )
        # decided event published for Worker
        decided = [
            (s, d) for s, d in pub.publishes if s.startswith("approval.decided.")
        ]
        assert len(decided) == 1
        assert decided[0][0] == f"approval.decided.{reqs[0].id}"

    async def test_member_requester_self_approves_pending_even_without_owner(
        self,
    ) -> None:
        """T2a.1 — v6.1 self-approval semantics replace the legacy
        three-tier fallback chain. Even when the policy lists an
        empty ``approver_user_ids`` AND the tenant has no owner,
        ``_resolve_approvers`` snapshots the requester so the row
        is pending (not skipped). The requester themselves can then
        decide; SoD is intentionally relinquished for the v6.1 cut.

        The legacy test asserted "skipped" — that branch is
        unreachable under v6.1 except when requester_user_id is
        empty, which ``handle_proposal``'s malformed-drop check
        already rules out before the resolve step.
        """
        t = await create_tenant(name="Tnone", slug=f"tn-{uuid.uuid4().hex[:8]}")
        # Member, not owner — proves we no longer fall back to owners.
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
            approver_user_ids=[],  # empty — pre-v6.1 this triggered skipped
        )

        engine, _pub, _ch = _engine()
        await _call_proposal(engine,
            {
                "tenantId": t.id,
                "coworkerId": cw.id,
                "conversationId": conv.id,
                "jobId": "job-self",
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
        req = reqs[0]
        assert req.status == "pending", (
            "v6.1 self-approval must keep the row pending so the "
            "requester can decide; legacy 'skipped' is gone"
        )
        # The requester is the sole resolved approver — not policy
        # approvers (empty), not tenant owners (none), not coworker
        # assignees (none). This is the v6.1 invariant.
        assert req.resolved_approvers == [member.id]


class TestResolveApprovers:
    """Direct exercise of the v6.1 self-approval primitive without
    relying on full handle_proposal plumbing. The function is the
    chokepoint where the legacy three-tier fallback was deleted; this
    class pins the new contract independently of caller wiring.
    """

    async def test_self_approval_returns_requester_only(self) -> None:
        """T2a.1 — non-empty requester → ``[requester]`` regardless
        of policy.approver_user_ids."""
        tenant_id, _user_id, cw_id, _conv_id, _job_id, policy_id = await _seed()
        engine, _pub, _ch = _engine()
        from rolemesh.db import get_approval_policy

        policy = await get_approval_policy(policy_id, tenant_id=tenant_id)
        assert policy is not None
        # Even though the policy lists user_id as the only approver,
        # the engine route is independent: it returns [requester_user_id].
        # Pick a non-requester to make the assertion sharp.
        non_requester = str(uuid.uuid4())
        out = await engine._resolve_approvers(
            tenant_id, cw_id, policy, requester_user_id=non_requester
        )
        assert out == [non_requester], (
            "self-approval must use the requester, not policy approvers"
        )

    async def test_empty_requester_returns_empty_list(self) -> None:
        """T2a.1 — empty requester → ``[]``. Caller (handle_auto_intercept
        or Case B fallback) is responsible for routing to the E path,
        not for inventing an approver."""
        tenant_id, _user_id, cw_id, _conv_id, _job_id, policy_id = await _seed()
        engine, _pub, _ch = _engine()
        from rolemesh.db import get_approval_policy

        policy = await get_approval_policy(policy_id, tenant_id=tenant_id)
        assert policy is not None
        out = await engine._resolve_approvers(
            tenant_id, cw_id, policy, requester_user_id=""
        )
        assert out == [], (
            "empty requester must propagate as [] so callers can "
            "route to the owner-FYI edge path"
        )


class TestSafetyDoesNotSelfApprove:
    """T2a.4 — the safety bridge intentionally bypasses self-approval
    (decision #12). A safety verdict's whole point is to take the
    decision out of the requester's hands; routing back to them would
    invert the gate. The test pins that the resolved_approvers on a
    safety-created row is tenant owners, never the requester.
    """

    async def test_safety_resolves_to_tenant_owners_not_requester(
        self,
    ) -> None:
        tenant_id, user_id, cw_id, conv_id, _job_id, _p = await _seed()
        # Add a non-owner who will be the safety event's "requester".
        member = await create_user(
            tenant_id=tenant_id, name="Mem", email="mem@x.com", role="member"
        )
        engine, _pub, _ch = _engine()
        req = await engine.create_from_safety(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            conversation_id=conv_id,
            job_id="safety-1",
            user_id=member.id,
            tool_name="dangerous",
            tool_input={"flag": True},
            mcp_server_name="safety-mcp",
        )
        assert req is not None
        # Owners list contains the seed-created owner (user_id), not
        # the requester (member). If a future refactor swaps the
        # safety path to use _resolve_approvers, the list would
        # collapse to [member.id] and this assertion would catch it.
        assert user_id in req.resolved_approvers
        assert member.id not in req.resolved_approvers, (
            "safety path must not self-approve — requester is recorded "
            "as user_id but not as an approver"
        )
        # Requester is still recorded on the row (for audit).
        assert req.user_id == member.id


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
        audit = await list_approval_audit(reqs[0].id, tenant_id=reqs[0].tenant_id)
        assert audit[0].action == "created"
        assert audit[0].actor_user_id is None


# ---------------------------------------------------------------------------
# v6.1 §P2.6 — edge fallback (empty requester) on auto_intercept
# ---------------------------------------------------------------------------


def _engine_with_owner_convs(
    owner_id_to_conv: dict[str, list[str]],
) -> tuple[ApprovalEngine, _FakePublisher, _FakeChannel]:
    """An engine variant whose resolver answers
    ``get_conversations_for_user_and_coworker`` from a fixed map. Used
    for the edge fallback tests where we need owners to have a
    deliverable conversation (otherwise the engine has nowhere to
    send the FYI and the assertion is meaningless).
    """
    pub = _FakePublisher()
    ch = _FakeChannel()

    async def _convs(user_id: str, _coworker_id: str) -> list[str]:
        return list(owner_id_to_conv.get(user_id, []))

    async def _get_conv(_conv_id: str) -> object | None:
        return object()

    resolver = NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_get_conv,
    )
    eng = ApprovalEngine(publisher=pub, channel_sender=ch, resolver=resolver)
    return eng, pub, ch


class TestAutoInterceptEdgeFallback:
    """T2a.2 / T2a.7 / T2a.8 / T2a.9 / T2a.10 — v6.1 §P2.6 wraps the
    five edge-path invariants. Keeping them in one class lets a future
    refactor that tries to satisfy any single one without honouring
    the others fail loudly together.
    """

    async def test_split_drops_only_when_server_or_tool_missing(self) -> None:
        """T2a.2 — the legacy combined check
        ``not user_id or not server or not tool`` is split. Empty
        ``server`` must still drop (we cannot identify what was
        intercepted); empty ``user_id`` must NOT drop — it has its
        own E-path handler now."""
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        engine, _pub, _ch = _engine()
        # Empty server is genuinely malformed.
        await _call_auto_intercept(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": "j-malformed",
                "userId": user_id,
                "mcp_server_name": "",  # missing
                "tool_name": "refund",
                "tool_params": {"amount": 1},
                "action_hash": "h",
            }
        )
        assert await list_approval_requests(tenant_id) == [], (
            "missing server must drop the message — engine cannot "
            "identify the intercepted action"
        )

    async def test_empty_user_id_sends_owner_fyi_without_db_row(
        self,
    ) -> None:
        """T2a.7 — empty requester (system / chained / bootstrap)
        triggers the E path: tenant owners are FYI'd, but **no**
        ``approval_requests`` row is created. The hook already
        fail-closed the call; surfacing a row would mislead operators
        into thinking they had a pending decision."""
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        # Make sure a matching policy exists so we actually reach the
        # E path (otherwise the policy-no-longer-matches branch wins).
        engine, _pub, ch = _engine_with_owner_convs({user_id: [conv_id]})
        await _call_auto_intercept(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": "j-edge",
                "userId": "",  # the v6.1 edge signal
                "mcp_server_name": "erp",
                "tool_name": "refund",
                "tool_params": {"amount": 5000},
                "action_hash": "h-edge",
            }
        )
        # No DB row was created — invariant #6.
        assert await list_approval_requests(tenant_id) == []
        # Exactly one FYI was sent to the owner's conversation.
        assert ch.sent, "owner FYI must be sent to a conversation"
        assert ch.sent[0][0] == conv_id
        body = ch.sent[0][1]
        # Plain text, no buttons — the format helper's signature line
        # carries no markdown action.
        assert "FYI" in body
        assert "erp/refund" in body

    async def test_no_owners_logs_error_and_returns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T2a.8 — no tenant owners → ERROR log + return, no FYI.
        Importantly: no exception leaks (the hook is upstream and
        cannot recover from one), and no DB row is written.

        We patch the engine module's logger so the ERROR-level call
        can be asserted directly. structlog's ``PrintLoggerFactory``
        with ``cache_logger_on_first_use=True`` caches a stderr
        reference at first use, defeating both ``capsys`` and
        ``capfd`` — so an in-test ``_RecordingLogger`` is the
        smallest way to pin the contract.
        """
        # Tenant with a non-owner member only.
        t = await create_tenant(name="Tno", slug=f"tno-{uuid.uuid4().hex[:8]}")
        member = await create_user(
            tenant_id=t.id, name="M", email="m@x.com", role="member"
        )
        cw = await create_coworker(
            tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
        )
        b = await create_channel_binding(
            coworker_id=cw.id, tenant_id=t.id,
            channel_type="telegram", credentials={"bot_token": "x"},
        )
        conv = await create_conversation(
            tenant_id=t.id, coworker_id=cw.id, channel_binding_id=b.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        await create_approval_policy(
            tenant_id=t.id, coworker_id=cw.id,
            mcp_server_name="erp", tool_name="refund",
            condition_expr={"always": True},
            approver_user_ids=[member.id],
        )

        engine, _pub, ch = _engine_with_owner_convs({})
        # Record every (level, message, kwargs) tuple to a list.
        records: list[tuple[str, str, dict[str, Any]]] = []

        class _RecordingLogger:
            def _record(self, level: str):
                def inner(msg: str, **kwargs: Any) -> None:
                    records.append((level, msg, kwargs))
                return inner

            def __getattr__(self, name: str):
                return self._record(name)

        from rolemesh.approval import engine as engine_module
        monkeypatch.setattr(engine_module, "logger", _RecordingLogger())

        await _call_auto_intercept(engine,
            {
                "tenantId": t.id,
                "coworkerId": cw.id,
                "conversationId": conv.id,
                "jobId": "j-no-owner",
                "userId": "",  # E path
                "mcp_server_name": "erp",
                "tool_name": "refund",
                "tool_params": {"amount": 5000},
                "action_hash": "h-no-owner",
            }
        )
        # No DB row, no FYI.
        assert await list_approval_requests(t.id) == []
        assert ch.sent == [], (
            "edge fallback without owners must not send any message"
        )
        # An ERROR-level log carrying the tenant id + the keywords
        # 'edge fallback' / 'no tenant owners'. Operators search for
        # those when triaging silent failures.
        errors = [
            (msg, kw) for level, msg, kw in records if level == "error"
        ]
        assert errors, (
            f"expected an error-level log; got records: {records}"
        )
        assert any(
            "edge fallback" in msg.lower() and "no tenant owners" in msg.lower()
            for msg, _ in errors
        ), f"expected 'edge fallback' + 'no tenant owners'; got: {errors}"
        # Tenant id must be in the kwargs so a structured-log
        # consumer can index by tenant.
        assert any(kw.get("tenant_id") == t.id for _, kw in errors), (
            f"expected tenant_id={t.id} in log kwargs; got: {errors}"
        )

    async def test_rate_limit_suppresses_second_fyi_within_window(
        self,
    ) -> None:
        """T2a.9 — the in-process LRU rate-limits FYIs per
        ``(tenant, coworker, server, tool)`` to one per 5-minute
        window. Without it, a misbehaving system turn loop would
        carpet-bomb tenant owners."""
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        engine, _pub, ch = _engine_with_owner_convs({user_id: [conv_id]})

        payload: dict[str, Any] = {
            "tenantId": tenant_id,
            "coworkerId": cw_id,
            "conversationId": conv_id,
            "jobId": "j-rl",
            "userId": "",
            "mcp_server_name": "erp",
            "tool_name": "refund",
            "tool_params": {"amount": 5000},
            # NOTE: action_hash must differ on each call so the DB
            # dedup path does not pre-empt the rate-limit check —
            # otherwise we would be measuring the wrong thing.
            "action_hash": "h-rl-1",
        }
        await _call_auto_intercept(engine, payload)
        payload2 = {**payload, "action_hash": "h-rl-2"}
        await _call_auto_intercept(engine, payload2)

        # Exactly one FYI delivered despite two upstream calls.
        assert len(ch.sent) == 1, (
            f"rate-limit must collapse the second call; got {ch.sent}"
        )

        # A different tool with the same other fields is a different
        # rate-limit key and must NOT be suppressed.
        payload3 = {**payload, "tool_name": "cancel_order", "action_hash": "h-rl-3"}
        await create_approval_policy(
            tenant_id=tenant_id, coworker_id=cw_id,
            mcp_server_name="erp", tool_name="cancel_order",
            condition_expr={"always": True},
            approver_user_ids=[user_id],
        )
        await _call_auto_intercept(engine, payload3)
        assert len(ch.sent) == 2, (
            "different (server, tool) keys must not share the rate "
            "limit; second tool should produce a fresh FYI"
        )

    async def test_create_skipped_no_longer_fires_on_empty_approvers(
        self,
    ) -> None:
        """T2a.10 — M1 decision: the legacy 'approvers empty →
        create_skipped' branch in handle_auto_intercept is removed.
        Reaching that condition now goes through the edge FYI path,
        which does NOT write a row. ``approval_requests`` table
        must stay empty after the call."""
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        engine, _pub, _ch = _engine_with_owner_convs({user_id: [conv_id]})
        await _call_auto_intercept(engine,
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": "j-no-skip",
                "userId": "",
                "mcp_server_name": "erp",
                "tool_name": "refund",
                "tool_params": {"amount": 5000},
                "action_hash": "h-no-skip",
            }
        )
        reqs = await list_approval_requests(tenant_id)
        assert all(r.status != "skipped" for r in reqs), (
            "create_skipped must no longer fire from the auto_intercept "
            "edge path"
        )
        # In fact no row at all was written — the engine-side
        # invariant is "edge → no row".
        assert reqs == []


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
            request_id=req.id,
            tenant_id=req.tenant_id,
            outcome="approved",
            user_id=user_id,
        )
        assert updated.status == "approved"
        assert any(
            s == f"approval.decided.{req.id}" for s, _d in pub.publishes
        )
        audit = await list_approval_audit(req.id, tenant_id=req.tenant_id)
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
            tenant_id=req.tenant_id,
            outcome="rejected",
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
            request_id=req.id,
            tenant_id=req.tenant_id,
            outcome="approved",
            user_id=user_id,
        )
        with pytest.raises(ConflictError):
            await engine.handle_decision(
                request_id=req.id,
                tenant_id=req.tenant_id,
                outcome="rejected",
                user_id=user_id,
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
                request_id=req.id,
                tenant_id=req.tenant_id,
                outcome="approved",
                user_id=outsider.id,
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
            request_id=first_id,
            tenant_id=reqs[0].tenant_id,
            outcome="approved",
            user_id=user_id,
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
        from rolemesh.db import create_approval_request, list_approval_policies

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
        after = await get_approval_request(req.id, tenant_id=req.tenant_id)
        assert after is not None and after.status == "expired"
        audit = await list_approval_audit(req.id, tenant_id=req.tenant_id)
        assert any(e.action == "expired" and e.actor_user_id is None for e in audit)

    async def test_reconcile_republishes_stuck_approved(self) -> None:
        tenant_id, user_id, cw_id, conv_id, _j, _p = await _seed()
        from rolemesh.db import create_approval_request, list_approval_policies

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
        from rolemesh.db import _get_pool

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
        from rolemesh.db import (
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
        after = await get_approval_request(req.id, tenant_id=req.tenant_id)
        assert after is not None and after.status == "execution_stale"
        audit = await list_approval_audit(req.id, tenant_id=req.tenant_id)
        assert any(e.action == "execution_stale" for e in audit)


# Silence ruff / unused-import warnings in the shared asyncio import kept
# around for future tests.
_ = (asyncio, json)
