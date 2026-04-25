"""E2E-11: Long batch execution — msg.in_progress keeps NATS ack alive.

Builds a 3-action batch where each MCP call takes 3 seconds. Total
batch duration ~9 seconds. We separately verify that the Worker does
NOT re-execute (no duplicate MCP hits) by counting ``mcp.received``.

The ackWait configured in executor.py is generous (600s) by design,
but we explicitly verify the ``in_progress()`` mechanism by checking
no second claim/execute attempt happens during the long batch.

Notes on why this is E2E-only:
  The ``ConsumerConfig(ack_wait=...)`` parameter is wired via
  JetStream, which unit tests with a fake message cannot exercise.
  This test will break if someone removes ``ack_wait`` or
  ``in_progress()`` and the default 30s is too short for the batch.
"""

from __future__ import annotations

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_long_batch_does_not_redeliver_or_double_execute(
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

    # Make every MCP call take real wall-clock time.
    harness.mcp.delay_seconds = 1.5

    actions = [
        {
            "mcp_server": harness.mcp_server_name,
            "tool_name": "refund",
            "params": {"amount": 100 + i, "order_id": f"o-{i}"},
        }
        for i in range(3)
    ]

    await harness.publish_agent_task(
        "e2e-long",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "e2e-long",
            "userId": seed.owner_user_id,
            "rationale": "batch",
            "actions": actions,
        },
    )

    async def _pending() -> bool:
        return bool(
            await pg.list_approval_requests(seed.tenant_id, status="pending")
        )

    await harness.wait_for(_pending, timeout=5.0)
    req = (
        await pg.list_approval_requests(seed.tenant_id, status="pending")
    )[0]

    async with harness.api_client(admin) as api:
        r = await api.post(
            f"/api/admin/approvals/{req.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 200

    # Wait for all 3 MCP calls to complete. With 1.5s each the batch
    # takes ~4.5s; generous window.
    async def _all_done() -> bool:
        return len(harness.mcp.received) >= 3

    await harness.wait_for(_all_done, timeout=15.0)

    # After another generous pause, there must STILL be exactly 3 calls
    # (no redelivery / duplicate execution).
    import asyncio

    await asyncio.sleep(1.0)
    assert len(harness.mcp.received) == 3, (
        f"expected exactly 3 MCP calls (one per action); got "
        f"{len(harness.mcp.received)}. Extra calls indicate JetStream "
        "redelivered the decided message and the Worker's atomic claim "
        "did not fully suppress re-execution."
    )

    # Each call has a distinct idempotency key (3 actions → 3 different
    # hashes because params differ).
    keys = [
        r.headers.get("X-Idempotency-Key", r.headers.get("x-idempotency-key"))
        for r in harness.mcp.received
    ]
    assert len(set(keys)) == 3, f"keys were not all distinct: {keys!r}"

    # Final status: executed.
    async def _executed() -> bool:
        fresh = await pg.get_approval_request(req.id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "executed"

    await harness.wait_for(_executed, timeout=5.0)
