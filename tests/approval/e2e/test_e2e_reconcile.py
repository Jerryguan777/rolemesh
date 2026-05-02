"""E2E-05: Worker missed the decided event → reconcile republishes.

Flow:
  1. A row is forced into ``status=approved`` without ever publishing
     ``approval.decided.<id>`` (simulates Worker offline during
     decide, or a NATS hiccup).
  2. Advance the row's ``updated_at`` into the past so the reconcile
     loop's "stuck approved" query picks it up.
  3. Trigger ``engine.reconcile_stuck_requests`` manually (the
     maintenance loop is already running with a 100ms interval in the
     test harness, but triggering is deterministic).
  4. Worker receives the republished event, claims, executes.

This exercises:
  - Real NATS publish from reconcile_stuck_requests
  - Real durable consumer delivery to the Worker
  - End-to-end state transition to executed with audit trail
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rolemesh.db import (
    _get_pool,
    create_approval_policy,
    create_approval_request,
    get_approval_request,
    list_approval_audit,
    set_approval_status,
)

from .harness import OrchestratorHarness, seed_tenant


async def test_reconcile_republishes_stuck_approved_to_executed(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()

    # Insert an approved row directly (skip engine.decide so we never
    # publish the decided event).
    policy = await create_approval_policy(
        tenant_id=seed.tenant_id,
        coworker_id=seed.coworker_id,
        mcp_server_name=harness.mcp_server_name,
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[seed.owner_user_id],
    )
    req = await create_approval_request(
        tenant_id=seed.tenant_id,
        coworker_id=seed.coworker_id,
        conversation_id=seed.conversation_id,
        policy_id=policy.id,
        user_id=seed.owner_user_id,
        job_id="e2e-reconcile",
        mcp_server_name=harness.mcp_server_name,
        actions=[
            {
                "mcp_server": harness.mcp_server_name,
                "tool_name": "refund",
                "params": {"amount": 99},
            }
        ],
        action_hashes=["hash-reconcile"],
        rationale="r",
        source="proposal",
        status="pending",
        resolved_approvers=[seed.owner_user_id],
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    # Flip to approved without publishing.
    await set_approval_status(req.id, "approved", tenant_id=seed.tenant_id)

    # Push updated_at into the past so the 60s grace window triggers.
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE approval_requests SET updated_at = now() - interval '2 minutes' "
            "WHERE id = $1::uuid",
            req.id,
        )

    # Explicit reconcile (also runs every 100ms from harness).
    await harness.engine.reconcile_stuck_requests()

    async def _mcp_hit() -> bool:
        return len(harness.mcp.received) >= 1

    await harness.wait_for(_mcp_hit, timeout=10.0)

    async def _executed() -> bool:
        fresh = await get_approval_request(req.id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "executed"

    await harness.wait_for(_executed, timeout=5.0)

    # Audit must reflect that the executing → executed transitions did
    # happen — reconcile's only job is to get the Worker running,
    # everything after that is the normal flow.
    audit_actions = [
        e.action for e in await list_approval_audit(req.id, tenant_id=seed.tenant_id)
    ]
    assert "executing" in audit_actions
    assert "executed" in audit_actions
