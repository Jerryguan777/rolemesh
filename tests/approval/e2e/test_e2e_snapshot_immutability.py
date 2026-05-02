"""E2E-09: Policy approvers edit does not re-scope open requests.

``resolved_approvers`` on an approval request is a snapshot taken at
creation time. If the admin edits the policy after the request is
created, the new approver list must NOT affect who is authorised to
decide the existing request.

Flow:
  1. Policy approver_user_ids = [alice]
  2. Agent proposes → row with resolved_approvers = [alice]
  3. Admin PATCHes policy to approver_user_ids = [bob]
  4. alice decides → 200 (she is in the snapshot)
  5. Create a second request (new agent task) → row with
     resolved_approvers = [bob] (the new snapshot)
  6. alice decides on the new row → 403 (she is not in the snapshot)
"""

from __future__ import annotations

from rolemesh.db import (
    create_user,
    list_approval_requests,
)

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_policy_approver_edit_does_not_re_scope_open_requests(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    # Create two named users: alice (first approver), bob (second).
    alice = await create_user(
        tenant_id=seed.tenant_id, name="Alice",
        email="alice@x.com", role="admin",
    )
    bob = await create_user(
        tenant_id=seed.tenant_id, name="Bob",
        email="bob@x.com", role="admin",
    )
    owner = make_auth_user(
        tenant_id=seed.tenant_id, user_id=seed.owner_user_id
    )
    alice_auth = make_auth_user(
        tenant_id=seed.tenant_id, user_id=alice.id, role="admin"
    )
    bob_auth = make_auth_user(
        tenant_id=seed.tenant_id, user_id=bob.id, role="admin"
    )

    # Policy with alice as approver.
    async with harness.api_client(owner) as api:
        r = await api.post(
            "/api/admin/approval-policies",
            json={
                "mcp_server_name": harness.mcp_server_name,
                "tool_name": "refund",
                "condition_expr": {"always": True},
                "coworker_id": seed.coworker_id,
                "approver_user_ids": [alice.id],
            },
        )
        assert r.status_code == 201
        policy_id = r.json()["id"]

    # Agent proposes → row_alice has resolved_approvers=[alice].
    await harness.publish_agent_task(
        "e2e-snap-1",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "e2e-snap-1",
            "userId": seed.owner_user_id,
            "rationale": "r",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 1},
                }
            ],
        },
    )

    async def _one_pending() -> bool:
        return (
            len(await list_approval_requests(seed.tenant_id, status="pending"))
            == 1
        )

    await harness.wait_for(_one_pending, timeout=5.0)
    row_alice = (
        await list_approval_requests(seed.tenant_id, status="pending")
    )[0]
    assert row_alice.resolved_approvers == [alice.id]

    # Admin PATCHes policy to [bob].
    async with harness.api_client(owner) as api:
        r = await api.patch(
            f"/api/admin/approval-policies/{policy_id}",
            json={"approver_user_ids": [bob.id]},
        )
        assert r.status_code == 200

    # alice decides row_alice → 200 (snapshot honored).
    async with harness.api_client(alice_auth) as api:
        r = await api.post(
            f"/api/admin/approvals/{row_alice.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 200, (
            "alice was in resolved_approvers when row_alice was created; "
            "editing the policy must not revoke her authority"
        )

    # Fresh proposal → row_bob has resolved_approvers=[bob].
    await harness.publish_agent_task(
        "e2e-snap-2",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "e2e-snap-2",
            "userId": seed.owner_user_id,
            "rationale": "r",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 2},
                }
            ],
        },
    )

    async def _second_pending() -> bool:
        rows = await list_approval_requests(
            seed.tenant_id, status="pending"
        )
        return len(rows) == 1  # old row is now approved

    await harness.wait_for(_second_pending, timeout=5.0)
    row_bob = (
        await list_approval_requests(seed.tenant_id, status="pending")
    )[0]
    assert row_bob.resolved_approvers == [bob.id]

    # alice tries row_bob → 403 (she is not in the new snapshot).
    async with harness.api_client(alice_auth) as api:
        r = await api.post(
            f"/api/admin/approvals/{row_bob.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 403

    # bob can approve row_bob → 200.
    async with harness.api_client(bob_auth) as api:
        r = await api.post(
            f"/api/admin/approvals/{row_bob.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 200
