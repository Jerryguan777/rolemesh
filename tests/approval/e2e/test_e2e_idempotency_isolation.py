"""E2E-04: X-Idempotency-Key cross-tenant collision.

Review hypothesis:
    ``compute_action_hash(tool_name, params)`` depends only on the
    tool and its arguments. Two different tenants that happen to call
    the same tool with the same arguments (e.g. ``refund
    amount=100``) produce byte-identical hashes. The Worker sends
    each as ``X-Idempotency-Key``. An MCP server that honors
    idempotency keys will treat the second caller's request as a
    replay of the first and return the first caller's cached result —
    leaking data across tenants.

What should happen:
    The hash input must include a tenant-scoped salt (e.g. the
    tenant_id or the approval request id) so the key is unique per
    tenant even when the semantic action is identical.

Outcome:
    Should FAIL against current main because
    ``compute_action_hash("refund", {"amount": 100})`` is the same
    regardless of tenant. The follow-up fix widens the hash input.
"""

from __future__ import annotations

from rolemesh.db import (
    list_approval_requests,
)

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_same_action_across_tenants_has_distinct_idempotency_keys(
    harness: OrchestratorHarness,
) -> None:
    tenant_a = await seed_tenant(name_prefix="TenantA")
    tenant_b = await seed_tenant(name_prefix="TenantB")
    admin_a = make_auth_user(
        tenant_id=tenant_a.tenant_id, user_id=tenant_a.owner_user_id
    )
    admin_b = make_auth_user(
        tenant_id=tenant_b.tenant_id, user_id=tenant_b.owner_user_id
    )

    # Identical policy in each tenant.
    for admin, seed in (
        (admin_a, tenant_a),
        (admin_b, tenant_b),
    ):
        async with harness.api_client(admin) as api:
            r = await api.post(
                "/api/admin/approval-policies",
                json={
                    "mcp_server_name": harness.mcp_server_name,
                    "tool_name": "refund",
                    "condition_expr": {"always": True},
                    "coworker_id": seed.coworker_id,
                    "approver_user_ids": [seed.owner_user_id],
                },
            )
            assert r.status_code == 201

    # Submit byte-identical actions from each tenant.
    same_action = {
        "mcp_server": harness.mcp_server_name,
        "tool_name": "refund",
        "params": {"amount": 100, "currency": "USD"},
    }
    for seed in (tenant_a, tenant_b):
        await harness.publish_agent_task(
            f"e2e-idem-{seed.tenant_id[:8]}",
            {
                "type": "submit_proposal",
                "tenantId": seed.tenant_id,
                "coworkerId": seed.coworker_id,
                "conversationId": seed.conversation_id,
                "jobId": f"e2e-idem-{seed.tenant_id[:8]}",
                "userId": seed.owner_user_id,
                "rationale": "collide",
                "actions": [same_action],
            },
        )

    async def _both_pending() -> bool:
        a = await list_approval_requests(tenant_a.tenant_id, status="pending")
        b = await list_approval_requests(tenant_b.tenant_id, status="pending")
        return len(a) == 1 and len(b) == 1

    await harness.wait_for(_both_pending, timeout=5.0)

    req_a = (
        await list_approval_requests(tenant_a.tenant_id, status="pending")
    )[0]
    req_b = (
        await list_approval_requests(tenant_b.tenant_id, status="pending")
    )[0]

    # Both approve.
    for admin, req in ((admin_a, req_a), (admin_b, req_b)):
        async with harness.api_client(admin) as api:
            r = await api.post(
                f"/api/admin/approvals/{req.id}/decide",
                json={"action": "approve"},
            )
            assert r.status_code == 200

    # Wait for both MCP calls.
    async def _both_executed() -> bool:
        return len(harness.mcp.received) == 2

    await harness.wait_for(_both_executed, timeout=10.0)

    keys = [
        r.headers.get("X-Idempotency-Key", r.headers.get("x-idempotency-key"))
        for r in harness.mcp.received
    ]
    # Both keys are present (non-empty).
    assert all(keys), f"every request must include an idempotency key; got {keys}"

    # THE ASSERTION: the keys MUST differ.
    assert keys[0] != keys[1], (
        "two tenants calling the same tool with the same arguments MUST "
        "produce distinct X-Idempotency-Key values. Current behavior lets "
        "an MCP server that honors idempotency (RFC 7231 §4.3.5.1 "
        "replay-safe) return tenant A's cached response to tenant B — "
        "a cross-tenant data leak. Fix compute_action_hash to include "
        "the tenant_id (or the approval request id) in its input."
    )
