"""E2E-08: Auto-intercept dedup lifecycle — rejected invalidates dedup.

The 5-minute dedup window in ``find_pending_request_by_action_hash``
only finds PENDING rows. After a rejection, the same agent hitting the
same blocked tool call again SHOULD create a fresh pending request —
the previous one is terminal, and the agent deserves another chance
(perhaps the approver wants to change their mind; dedup would hide
the retry).

Flow:
  1. auto_intercept with hash=H → row 1 (pending)
  2. approver rejects row 1
  3. auto_intercept with hash=H within 5 minutes → row 2 (pending)

Assert we have two rows, not one.
"""

from __future__ import annotations

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_dedup_does_not_suppress_retry_after_rejection(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    admin = make_auth_user(
        tenant_id=seed.tenant_id, user_id=seed.owner_user_id
    )
    async with harness.api_client(admin) as api:
        await api.post(
            "/api/admin/approval-policies",
            json={
                "mcp_server_name": harness.mcp_server_name,
                "tool_name": "refund",
                "condition_expr": {"always": True},
                "coworker_id": seed.coworker_id,
                "approver_user_ids": [seed.owner_user_id],
            },
        )

    payload_base = {
        "type": "auto_approval_request",
        "tenantId": seed.tenant_id,
        "coworkerId": seed.coworker_id,
        "conversationId": seed.conversation_id,
        "jobId": "e2e-dedup-life",
        "userId": seed.owner_user_id,
        "mcp_server_name": harness.mcp_server_name,
        "tool_name": "refund",
        "tool_params": {"amount": 5000, "order_id": "o-1"},
        "action_hash": "dedup-hash-x",
    }

    # First intercept → row 1.
    await harness.publish_agent_task("e2e-dedup-life", payload_base)

    async def _one_pending() -> bool:
        return (
            len(await pg.list_approval_requests(seed.tenant_id, status="pending"))
            == 1
        )

    await harness.wait_for(_one_pending, timeout=5.0)
    row1 = (
        await pg.list_approval_requests(seed.tenant_id, status="pending")
    )[0]

    # Approver rejects row 1.
    async with harness.api_client(admin) as api:
        r = await api.post(
            f"/api/admin/approvals/{row1.id}/decide",
            json={"action": "reject", "note": "not now"},
        )
        assert r.status_code == 200

    async def _row1_rejected() -> bool:
        fresh = await pg.get_approval_request(row1.id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "rejected"

    await harness.wait_for(_row1_rejected, timeout=5.0)

    # Agent retries with exactly the same hash. Because the previous
    # row is no longer pending, dedup must NOT block this one.
    await harness.publish_agent_task("e2e-dedup-life-2", payload_base)

    async def _two_rows() -> bool:
        return (
            len(await pg.list_approval_requests(seed.tenant_id)) == 2
        )

    await harness.wait_for(_two_rows, timeout=5.0)

    # The second row should be pending, distinct from row1.
    all_rows = await pg.list_approval_requests(seed.tenant_id)
    pending_rows = [r for r in all_rows if r.status == "pending"]
    rejected_rows = [r for r in all_rows if r.status == "rejected"]
    assert len(pending_rows) == 1
    assert len(rejected_rows) == 1
    assert pending_rows[0].id != rejected_rows[0].id
