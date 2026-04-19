"""E2E-10: A container claiming a different tenant_id cannot spoof.

Defense-in-depth test for the P1 fix landed in this branch:
  - Outer IPC dispatcher resolves the coworker by ``coworkerId`` and
    overrides ``tenantId`` from the resolved coworker's own record.
  - Engine's ``_tenant_matches`` guard as a second check.

Scenario:
    Tenant A's agent publishes ``auto_approval_request`` on its own
    NATS subject but stuffs tenant B's UUID in the body. The
    orchestrator must NOT create any approval request in tenant B —
    and should also not create one incorrectly attributed to A (since
    the body's tenantId is inconsistent with the resolved coworker).

Expected outcome:
    Either silently dropped (current behaviour: dispatcher overrides
    with the resolved tenant A's UUID, engine guard sees the body
    tenantId disagree → drop) or the request is created under A
    correctly.

What we MUST NOT observe:
    A new row in B's approval_requests table.
"""

from __future__ import annotations

import asyncio

from rolemesh.db import pg

from .harness import OrchestratorHarness, seed_tenant


async def test_forged_tenant_id_does_not_leak_across_tenants(
    harness: OrchestratorHarness,
) -> None:
    tenant_a = await seed_tenant(name_prefix="Atk")
    tenant_b = await seed_tenant(name_prefix="Vic")
    # B has a policy that would match; make sure if the forge DID work,
    # a row would actually get created.
    from rolemesh.db.pg import create_approval_policy

    await create_approval_policy(
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
        mcp_server_name=harness.mcp_server_name,
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[tenant_b.owner_user_id],
    )
    # A also has a policy (so we know A's side is healthy).
    await create_approval_policy(
        tenant_id=tenant_a.tenant_id,
        coworker_id=tenant_a.coworker_id,
        mcp_server_name=harness.mcp_server_name,
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[tenant_a.owner_user_id],
    )

    # Agent in tenant A publishes with forged body tenantId=B.
    await harness.publish_agent_task(
        "e2e-forge",
        {
            "type": "auto_approval_request",
            "tenantId": tenant_b.tenant_id,  # forged
            "coworkerId": tenant_a.coworker_id,  # real (belongs to A)
            "conversationId": tenant_a.conversation_id,
            "jobId": "e2e-forge",
            "userId": tenant_a.owner_user_id,
            "mcp_server_name": harness.mcp_server_name,
            "tool_name": "refund",
            "tool_params": {"amount": 9999},
            "action_hash": "forge-hash",
        },
    )

    # Plenty of wall-clock so NATS + dispatcher process.
    await asyncio.sleep(1.0)

    # THE ASSERTION: B sees nothing.
    b_rows = await pg.list_approval_requests(tenant_b.tenant_id)
    assert b_rows == [], (
        f"forged tenantId leaked a row into the victim tenant: {b_rows!r}"
    )

    # The dispatcher rewrites body.tenantId to the coworker's actual
    # tenant_id (A). Engine then sees claimed=B vs trusted=A → drops
    # with "mismatched tenantId" warning. So A also sees nothing.
    a_rows = await pg.list_approval_requests(tenant_a.tenant_id)
    assert a_rows == [], (
        "forge attempt should be dropped entirely (claimed tenantId "
        "disagreed with resolved trusted tenant), not silently "
        f"re-attributed to A: {a_rows!r}"
    )

    # Proof that A's side WOULD work with a consistent payload.
    await harness.publish_agent_task(
        "e2e-legit",
        {
            "type": "auto_approval_request",
            "tenantId": tenant_a.tenant_id,  # consistent
            "coworkerId": tenant_a.coworker_id,
            "conversationId": tenant_a.conversation_id,
            "jobId": "e2e-legit",
            "userId": tenant_a.owner_user_id,
            "mcp_server_name": harness.mcp_server_name,
            "tool_name": "refund",
            "tool_params": {"amount": 5000},
            "action_hash": "legit-hash",
        },
    )

    async def _a_has_one() -> bool:
        return (
            len(await pg.list_approval_requests(tenant_a.tenant_id)) == 1
        )

    await harness.wait_for(_a_has_one, timeout=5.0)
    # B is still empty after the legitimate A message.
    assert await pg.list_approval_requests(tenant_b.tenant_id) == []
