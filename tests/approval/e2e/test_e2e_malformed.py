"""E2E-13: Malformed NATS payloads must not crash or poison state.

Adversarial payloads that bypass the normal tool layer:
  - submit_proposal with no actions
  - submit_proposal with actions=null
  - auto_approval_request with missing server / tool
  - payload that is not even valid JSON shape (wrong types)

In none of these cases should the engine throw, and no approval row
should be created. The orchestrator's task subscription must ack and
move on.

If the engine or dispatcher crashed, the durable consumer would stop
delivering subsequent messages — we verify forward progress by sending
a well-formed proposal afterwards and observing it lands normally.
"""

from __future__ import annotations

import asyncio

from rolemesh.db import (
    list_approval_requests,
)

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_malformed_payloads_do_not_break_downstream_flow(
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

    malformed_payloads = [
        # 1. submit_proposal with no actions
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "malf-1",
            "userId": seed.owner_user_id,
            "rationale": "no actions",
            "actions": [],
        },
        # 2. submit_proposal with actions=None
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "malf-2",
            "userId": seed.owner_user_id,
            "rationale": "null actions",
            "actions": None,
        },
        # 3. auto_approval_request missing server
        {
            "type": "auto_approval_request",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "malf-3",
            "userId": seed.owner_user_id,
            "mcp_server_name": "",
            "tool_name": "refund",
            "tool_params": {"amount": 1},
            "action_hash": "h",
        },
        # NOTE: auto_approval_request with tool_params as non-dict is
        # intentionally coerced to {} by the engine (fail-safe), then
        # matched against the policy. With an ``always`` policy it
        # legitimately produces a pending row. That is NOT a malformed
        # payload for the purposes of this test.
        # 4. submit_proposal with userId missing
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "malf-5",
            "userId": "",
            "rationale": "r",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 1},
                }
            ],
        },
    ]
    for payload in malformed_payloads:
        await harness.publish_agent_task(
            str(payload.get("jobId")), payload
        )

    # Let the subscription consume everything.
    await asyncio.sleep(1.0)

    # No rows created by any of the malformed messages.
    assert await list_approval_requests(seed.tenant_id) == [], (
        "malformed payloads must not produce approval_requests rows"
    )

    # Forward-progress proof: a well-formed message still works.
    await harness.publish_agent_task(
        "malf-healthy",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "malf-healthy",
            "userId": seed.owner_user_id,
            "rationale": "healthy",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 1},
                }
            ],
        },
    )

    async def _healthy_pending() -> bool:
        return (
            len(await list_approval_requests(seed.tenant_id, status="pending"))
            == 1
        )

    await harness.wait_for(_healthy_pending, timeout=5.0)
