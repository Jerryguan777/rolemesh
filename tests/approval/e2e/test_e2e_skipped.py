"""E2E-12: Skipped path — policy with no resolvable approver.

When the fallback chain
    policy.approver_user_ids → user_agent_assignments → tenant owners
resolves to an empty list, the request must be created with
``status=skipped`` and the originating conversation must receive a
"please configure an approver" notification. The MCP server must
NEVER be called.

Sets up a tenant with NO owner role and a policy with empty
approver_user_ids, then asserts the full flow.
"""

from __future__ import annotations

import uuid

from rolemesh.db import pg

from .harness import OrchestratorHarness


async def test_proposal_skipped_when_no_approver_configured(
    harness: OrchestratorHarness,
) -> None:
    # Build a tenant with a member-only user (no owner).
    t = await pg.create_tenant(
        name="NoOwner", slug=f"no-{uuid.uuid4().hex[:8]}"
    )
    member = await pg.create_user(
        tenant_id=t.id, name="Mem", email="m@x.com", role="member"
    )
    cw = await pg.create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    b = await pg.create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    conv = await pg.create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    await pg.create_approval_policy(
        tenant_id=t.id,
        coworker_id=cw.id,
        mcp_server_name=harness.mcp_server_name,
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[],  # no explicit approver
    )
    # No user_agent_assignments, no tenant owner → fallback yields [].

    await harness.publish_agent_task(
        "e2e-skip",
        {
            "type": "submit_proposal",
            "tenantId": t.id,
            "coworkerId": cw.id,
            "conversationId": conv.id,
            "jobId": "e2e-skip",
            "userId": member.id,
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

    async def _skipped() -> bool:
        rows = await pg.list_approval_requests(t.id)
        return len(rows) == 1 and rows[0].status == "skipped"

    await harness.wait_for(_skipped, timeout=5.0)
    row = (await pg.list_approval_requests(t.id))[0]

    # Audit chain: created + skipped (trigger writes both on initial
    # non-pending INSERT).
    audit = [e.action for e in await pg.list_approval_audit(row.id)]
    assert audit == ["created", "skipped"], audit

    # Origin got a "no approver" style notification.
    assert any(
        m.conversation_id == conv.id
        and ("approver" in m.text.lower() or "not proceed" in m.text.lower())
        for m in harness.channel.messages
    )

    # MCP never invoked.
    import asyncio

    await asyncio.sleep(0.3)
    assert harness.mcp.received == []
