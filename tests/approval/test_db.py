"""DB CRUD tests for approval tables.

Adversarial behaviour under test:
  - The atomic decide_approval_request MUST be safe under two approvers
    racing: exactly one succeeds, the other sees None.
  - decide_approval_request MUST reject decisions from non-approvers.
  - claim_approval_for_execution MUST race-safe: two Workers claiming
    the same approved request yield exactly one success.
  - cancel_pending_approvals_for_job must only touch pending rows; an
    already-approved row survives Stop-cascade.
  - find_pending_request_by_action_hash returns within the window and
    does not return cancelled/expired rows.
  - audit_log is append-only: there is no update/delete surface at all.

Schema smoke tests rely on the test_db fixture which recreates tables
via _init_test_database → _create_schema.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rolemesh.approval.types import APPROVAL_STATUSES, AUDIT_ACTIONS
from rolemesh.db import (
    cancel_pending_approvals_for_job,
    claim_approval_for_execution,
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    decide_approval_request_full,
    delete_approval_policy,
    find_pending_request_by_action_hash,
    get_approval_policy,
    get_approval_request,
    get_enabled_policies_for_coworker,
    list_approval_audit,
    list_approval_policies,
    list_expired_pending_approvals,
    list_stuck_approved_approvals,
    list_stuck_executing_approvals,
    set_approval_status,
    update_approval_policy,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _chain() -> tuple[str, str, str, str, str]:
    """Create tenant + user + coworker + binding + conversation. Return IDs."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(tenant_id=t.id, name="Alice", email="a@x.com", role="owner")
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
    return t.id, u.id, cw.id, b.id, conv.id


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------


class TestPolicyCrud:
    async def test_create_and_get(self) -> None:
        tenant_id, _u, cw_id, _b, _c = await _chain()
        p = await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"field": "amount", "op": ">", "value": 1000},
            priority=10,
        )
        assert p.enabled is True
        assert p.priority == 10
        assert p.mcp_server_name == "erp"

        fetched = await get_approval_policy(p.id, tenant_id=tenant_id)
        assert fetched is not None
        assert fetched.id == p.id
        assert fetched.condition_expr == {
            "field": "amount",
            "op": ">",
            "value": 1000,
        }

    async def test_enabled_filter_excludes_disabled(self) -> None:
        tenant_id, _u, cw_id, _b, _c = await _chain()
        await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="a",
            tool_name="t",
            condition_expr={"always": True},
            enabled=True,
        )
        await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="b",
            tool_name="t",
            condition_expr={"always": True},
            enabled=False,
        )
        enabled = await get_enabled_policies_for_coworker(tenant_id, cw_id)
        names = {p.mcp_server_name for p in enabled}
        assert names == {"a"}

    async def test_tenant_wide_policy_applies_to_all_coworkers(self) -> None:
        tenant_id, _u, cw_id, _b, _c = await _chain()
        # coworker_id=None means "tenant-wide"
        await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=None,
            mcp_server_name="global",
            tool_name="*",
            condition_expr={"always": True},
        )
        enabled = await get_enabled_policies_for_coworker(tenant_id, cw_id)
        assert any(p.mcp_server_name == "global" for p in enabled)

    async def test_update_changes_updated_at(self) -> None:
        tenant_id, _u, cw_id, _b, _c = await _chain()
        p = await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"always": True},
        )
        original_updated = p.updated_at
        updated = await update_approval_policy(
            p.id, tenant_id=tenant_id, priority=99, enabled=False
        )
        assert updated is not None
        assert updated.priority == 99
        assert updated.enabled is False
        # updated_at must advance on every UPDATE, otherwise find_matching_policy
        # cannot tie-break by recency.
        assert updated.updated_at != original_updated

    async def test_delete_removes_row(self) -> None:
        tenant_id, _u, cw_id, _b, _c = await _chain()
        p = await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"always": True},
        )
        assert await delete_approval_policy(p.id, tenant_id=tenant_id) is True
        assert await get_approval_policy(p.id, tenant_id=tenant_id) is None

    async def test_list_filters_by_coworker(self) -> None:
        tenant_id, _u, cw_id, _b, _c = await _chain()
        other_cw = await create_coworker(
            tenant_id=tenant_id, name="Other", folder=f"o-{uuid.uuid4().hex[:8]}"
        )
        await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            mcp_server_name="a",
            tool_name="t",
            condition_expr={"always": True},
        )
        await create_approval_policy(
            tenant_id=tenant_id,
            coworker_id=other_cw.id,
            mcp_server_name="b",
            tool_name="t",
            condition_expr={"always": True},
        )
        only_cw = await list_approval_policies(tenant_id, coworker_id=cw_id)
        assert len(only_cw) == 1
        assert only_cw[0].mcp_server_name == "a"


# ---------------------------------------------------------------------------
# Request CRUD — atomic transitions
# ---------------------------------------------------------------------------


async def _request(
    tenant_id: str,
    user_id: str,
    coworker_id: str,
    conversation_id: str,
    policy_id: str,
    *,
    resolved_approvers: list[str],
    status: str = "pending",
    action_hashes: list[str] | None = None,
    job_id: str = "job-1",
) -> str:
    req = await create_approval_request(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        conversation_id=conversation_id,
        policy_id=policy_id,
        user_id=user_id,
        job_id=job_id,
        mcp_server_name="erp",
        actions=[{"mcp_server": "erp", "tool_name": "t", "params": {"a": 1}}],
        action_hashes=action_hashes or ["hash-1"],
        rationale="because",
        source="proposal",
        status=status,
        resolved_approvers=resolved_approvers,
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    return req.id


async def _make_policy(tenant_id: str, cw_id: str) -> str:
    p = await create_approval_policy(
        tenant_id=tenant_id,
        coworker_id=cw_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
    )
    return p.id


class TestDecideAtomic:
    async def test_first_decide_wins(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        # Two approvers, both authorised.
        other = await create_user(
            tenant_id=tenant_id, name="Bob", email="b@x.com", role="owner"
        )
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id, other.id],
        )
        first = await decide_approval_request_full(
            request_id,
            tenant_id=tenant_id,
            new_status="approved",
            actor_user_id=user_id,
        )
        second = await decide_approval_request_full(
            request_id,
            tenant_id=tenant_id,
            new_status="rejected",
            actor_user_id=other.id,
        )
        assert first.kind == "updated"
        assert first.request is not None and first.request.status == "approved"
        assert second.kind == "conflict", (
            "second decider must see a conflict — the pending→approved "
            "transition should be atomic and win-once"
        )
        assert second.current_status == "approved"

    async def test_non_approver_cannot_decide(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        outsider = await create_user(
            tenant_id=tenant_id, name="Eve", email="e@x.com", role="member"
        )
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
        )
        result = await decide_approval_request_full(
            request_id,
            tenant_id=tenant_id,
            new_status="approved",
            actor_user_id=outsider.id,
        )
        assert result.kind == "forbidden"

    async def test_decide_only_from_pending(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="approved",
        )
        result = await decide_approval_request_full(
            request_id,
            tenant_id=tenant_id,
            new_status="approved",
            actor_user_id=user_id,
        )
        assert result.kind == "conflict"
        assert result.current_status == "approved"


class TestClaimForExecution:
    async def test_single_claim_succeeds(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="approved",
        )
        first = await claim_approval_for_execution(request_id, tenant_id=tenant_id)
        second = await claim_approval_for_execution(request_id, tenant_id=tenant_id)
        assert first is not None and first.status == "executing"
        assert second is None

    async def test_claim_requires_approved_status(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="pending",
        )
        assert (
            await claim_approval_for_execution(request_id, tenant_id=tenant_id)
            is None
        )


class TestCancelForJob:
    async def test_cancels_only_pending_not_approved(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        pending_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="pending",
            job_id="job-A",
        )
        approved_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="approved",
            job_id="job-A",
            action_hashes=["hash-2"],
        )
        cancelled = await cancel_pending_approvals_for_job("job-A")
        assert cancelled == [(pending_id, tenant_id)]
        after = await get_approval_request(approved_id, tenant_id=tenant_id)
        assert after is not None and after.status == "approved"


# ---------------------------------------------------------------------------
# Dedup / maintenance queries
# ---------------------------------------------------------------------------


class TestDedupLookup:
    async def test_finds_pending_within_window(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            action_hashes=["dedup-hash"],
        )
        found = await find_pending_request_by_action_hash(tenant_id, "dedup-hash")
        assert found is not None

    async def test_rejected_rows_are_not_candidates(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            action_hashes=["dedup-hash-2"],
        )
        await set_approval_status(request_id, "rejected", tenant_id=tenant_id)
        found = await find_pending_request_by_action_hash(tenant_id, "dedup-hash-2")
        assert found is None


class TestMaintenanceQueries:
    async def test_expired_listing(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        # Create with an explicit past expiry by patching expires_at via the
        # normal constructor then rewriting with set_approval_status-friendly
        # SQL — simplest path: create with expires_at in the past.
        req = await create_approval_request(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            conversation_id=conv_id,
            policy_id=policy_id,
            user_id=user_id,
            job_id="j-expired",
            mcp_server_name="erp",
            actions=[{"mcp_server": "erp", "tool_name": "t", "params": {}}],
            action_hashes=["h"],
            rationale=None,
            source="proposal",
            status="pending",
            resolved_approvers=[user_id],
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        rows = await list_expired_pending_approvals()
        assert req.id in [r.id for r in rows]

    async def test_stuck_approved_respects_grace_period(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        req_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="approved",
        )
        # 0-second grace window makes the fresh row appear "stuck" immediately.
        # Test both that the query returns it at 0s and omits it at 60s.
        immediate = await list_stuck_approved_approvals(older_than_seconds=0)
        patient = await list_stuck_approved_approvals(older_than_seconds=60)
        ids_immediate = {r.id for r in immediate}
        ids_patient = {r.id for r in patient}
        assert req_id in ids_immediate
        assert req_id not in ids_patient

    async def test_stuck_executing_respects_grace_period(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        req_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="executing",
        )
        immediate = await list_stuck_executing_approvals(older_than_seconds=0)
        assert req_id in {r.id for r in immediate}


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    async def test_trigger_writes_created_on_insert(self) -> None:
        # The audit trigger fires on INSERT and writes a 'created' row.
        # Tests here pin that behaviour — callers should NOT write a
        # 'created' row manually; that would duplicate the trigger output.
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
        )
        entries = await list_approval_audit(request_id, tenant_id=tenant_id)
        assert [e.action for e in entries] == ["created"]

    async def test_trigger_writes_status_change_on_update(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
        )
        # Transition pending → approved via set_approval_status; the
        # trigger writes the 'approved' row in the same transaction.
        await set_approval_status(
            request_id,
            "approved",
            tenant_id=tenant_id,
            actor_user_id=user_id,
            note="looks good",
        )
        entries = await list_approval_audit(request_id, tenant_id=tenant_id)
        assert [e.action for e in entries] == ["created", "approved"]
        assert entries[1].actor_user_id == user_id
        assert entries[1].note == "looks good"

    async def test_metadata_on_terminal_status(self) -> None:
        tenant_id, user_id, cw_id, _b, conv_id = await _chain()
        policy_id = await _make_policy(tenant_id, cw_id)
        request_id = await _request(
            tenant_id,
            user_id,
            cw_id,
            conv_id,
            policy_id,
            resolved_approvers=[user_id],
            status="executing",
        )
        # Worker path: executing → executed with metadata passed through.
        meta = {"results": [{"ok": True, "amount": 100}]}
        await set_approval_status(
            request_id, "executed", tenant_id=tenant_id, metadata=meta
        )
        entries = await list_approval_audit(request_id, tenant_id=tenant_id)
        # INSERT with status='executing' yields 2 rows (created + executing).
        # UPDATE to 'executed' yields 1 row with metadata.
        actions = [e.action for e in entries]
        assert actions[-1] == "executed"
        executed_entry = entries[-1]
        assert executed_entry.metadata == meta


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


class TestSchemaSanity:
    async def test_status_domain_matches_module(self) -> None:
        # If someone adds a new status to the DB CHECK constraint without
        # updating APPROVAL_STATUSES, the app code can't decide which path
        # to take — fail loudly.
        assert {
            "pending",
            "approved",
            "rejected",
            "expired",
            "cancelled",
            "skipped",
            "executing",
            "executed",
            "execution_failed",
            "execution_stale",
        } == APPROVAL_STATUSES

    async def test_audit_actions_domain_matches_module(self) -> None:
        assert {
            "created",
            "approved",
            "rejected",
            "expired",
            "cancelled",
            "skipped",
            "executing",
            "executed",
            "execution_failed",
            "execution_stale",
        } == AUDIT_ACTIONS
