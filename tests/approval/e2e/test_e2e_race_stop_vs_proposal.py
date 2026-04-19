"""E2E-02: Stop cascade arrives before the proposal it should cancel.

Known limitation (scope-decision 2026-04-19):
    NATS does not guarantee cross-subject ordering. If
    ``approval.cancel_for_job.<J>`` lands before ``submit_proposal``
    on ``agent.<J>.tasks``, ``cancel_for_job`` finds nothing to
    cancel and the proposal subsequently creates a pending row that
    is no longer attached to any running turn.

    The decision was to NOT add a ``cancelled_jobs`` tracking table
    for this window. Rationale:

      1. Approvers act on pending requests in wall-clock minutes.
         A human seeing an approval notification for an already-
         stopped turn is visually detectable ("the chat said it was
         stopped, but this approval is waiting for me").
      2. Every policy carries ``auto_expire_minutes`` (default 60m).
         The maintenance loop transitions the orphan to ``expired``
         and sends a notification without any additional machinery.
      3. The cost of defense-in-depth (a second state table + an
         extra SQL check on every create path) was judged larger
         than the realistic harm.

    If this tradeoff becomes wrong (e.g. an auto-approver robot
    approves orphan requests before an operator notices), swap this
    test's expectation and add the tracking table.

This test documents the current behaviour: after a cancel+proposal
race, an orphan pending row exists, and it is reaped by the normal
expiry loop — not by the Stop cascade.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_cancel_then_late_proposal_creates_orphan_that_expires(
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
                # Short expiry so the test exercises the reaper.
                "auto_expire_minutes": 1,
            },
        )
        assert r.status_code == 201

    job_id = "e2e-race-stop"

    # Cancel arrives first.
    await harness.publish_cancel(job_id)
    await asyncio.sleep(0.3)

    # Proposal arrives late.
    await harness.publish_agent_task(
        job_id,
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": job_id,
            "userId": seed.owner_user_id,
            "rationale": "late — job was already stopped",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 42},
                }
            ],
        },
    )

    async def _one_row() -> bool:
        return len(await pg.list_approval_requests(seed.tenant_id)) == 1

    await harness.wait_for(_one_row, timeout=5.0)
    row = (await pg.list_approval_requests(seed.tenant_id))[0]

    # CURRENT BEHAVIOUR (documented limitation): the orphan is pending.
    # Stop cascade did not reach it because the row did not exist yet
    # when cancel_for_job ran. The MCP server has NOT been called
    # (pending requests never execute), so the "damage" is contained
    # to an approver seeing a stale request.
    assert row.status == "pending"
    assert row.job_id == job_id
    assert harness.mcp.received == [], (
        "pending requests must not execute regardless of orphan status"
    )

    # EXPIRY PATH: force the row past its deadline and trigger the
    # maintenance loop. The orphan transitions to ``expired``, gets
    # an audit row, and the origin conversation is notified — the
    # same path any regular pending request takes.
    pool = pg._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE approval_requests SET expires_at = $1 "
            "WHERE id = $2::uuid",
            datetime.now(UTC) - timedelta(seconds=1),
            row.id,
        )
    await harness.engine.expire_stale_requests()

    fresh = await pg.get_approval_request(row.id)
    assert fresh is not None
    assert fresh.status == "expired", (
        f"orphan pending row must be reaped by expiry; got {fresh.status!r}"
    )
    # Audit captures the expire as a system transition.
    expired_audit = [
        e for e in await pg.list_approval_audit(row.id)
        if e.action == "expired"
    ]
    assert len(expired_audit) == 1
    assert expired_audit[0].actor_user_id is None
    # Origin got a notification about the expire.
    assert any(
        m.conversation_id == seed.conversation_id
        and "expired" in m.text.lower()
        for m in harness.channel.messages
    )
