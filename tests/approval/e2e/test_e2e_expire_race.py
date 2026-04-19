"""E2E-07: Expire vs approve race — CAS wins once.

Both the maintenance loop (expire_stale_requests) and an approver's
decide run the same pending→terminal SQL transition. If the row is
past its deadline AND the approver is clicking approve at the same
millisecond, exactly one must succeed.

This exercises the Postgres ``UPDATE ... WHERE status='pending'`` CAS
pattern under concurrent contention.

We force the race by:
  1. Creating a pending row with ``expires_at`` in the past
  2. Running expire + decide concurrently via asyncio.gather

Then assert the terminal state is exactly one of (expired | approved)
and the audit chain has exactly one terminal row (not both).
"""

from __future__ import annotations

import asyncio

import pytest

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_concurrent_expire_and_approve_yield_single_terminal(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    admin = make_auth_user(
        tenant_id=seed.tenant_id, user_id=seed.owner_user_id
    )
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

    await harness.publish_agent_task(
        "e2e-expire",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "e2e-expire",
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

    async def _pending() -> bool:
        return bool(
            await pg.list_approval_requests(seed.tenant_id, status="pending")
        )

    await harness.wait_for(_pending, timeout=5.0)
    req = (
        await pg.list_approval_requests(seed.tenant_id, status="pending")
    )[0]

    # Force the row past its deadline.
    pool = pg._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE approval_requests SET expires_at = now() - interval '1 second' "
            "WHERE id = $1::uuid",
            req.id,
        )

    # Run both CAS attempts concurrently.
    async def _approver_decides() -> int:
        async with harness.api_client(admin) as api:
            r = await api.post(
                f"/api/admin/approvals/{req.id}/decide",
                json={"action": "approve"},
            )
            return r.status_code

    async def _expire_step() -> int:
        return await harness.engine.expire_stale_requests()

    decide_status, expire_count = await asyncio.gather(
        _approver_decides(), _expire_step()
    )

    # Exactly one must have seen the pending row.
    if decide_status == 200:
        # Approver won the race.
        assert expire_count == 0, (
            "expire must not have transitioned a row that was already approved"
        )
        fresh = await pg.get_approval_request(req.id)
        assert fresh is not None and fresh.status == "approved"
    elif decide_status == 409:
        # Expire won the race. Decide saw the row as no longer pending.
        assert expire_count == 1
        fresh = await pg.get_approval_request(req.id)
        assert fresh is not None and fresh.status == "expired"
    else:
        pytest.fail(
            f"Unexpected decide status {decide_status}; should be 200 or 409"
        )

    # No matter who won, audit must contain exactly one terminal row
    # for this request.
    audit_actions = [
        e.action for e in await pg.list_approval_audit(req.id)
    ]
    terminals = [
        a for a in audit_actions if a in ("approved", "expired", "rejected")
    ]
    assert len(terminals) == 1, (
        f"audit must have exactly one terminal row; got {audit_actions!r}"
    )
