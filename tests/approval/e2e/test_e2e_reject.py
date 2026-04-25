"""E2E-06: Reject with note — origin gets notification, MCP never hit.

Covers the "human said no" path end to end:
  - decide REST with action=reject + note
  - engine publishes approval.decided.<id> with status=rejected + note
  - Worker consumes, sees status=rejected, does NOT claim / execute
  - Worker sends a rejection message to the originating conversation
    containing the approver's note

Also asserts the audit chain is ``[created, rejected]`` and nothing
leaks into the execution path.
"""

from __future__ import annotations

import asyncio

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_reject_delivers_note_and_never_calls_mcp(
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

    await harness.publish_agent_task(
        "e2e-reject",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "e2e-reject",
            "userId": seed.owner_user_id,
            "rationale": "pls",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 1000},
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

    # Clear the notifications recorded so far (approver notify) so we
    # can cleanly observe the post-reject message.
    prev_notifications = len(harness.channel.messages)

    async with harness.api_client(admin) as api:
        r = await api.post(
            f"/api/admin/approvals/{req.id}/decide",
            json={
                "action": "reject",
                "note": "not this quarter — budget freeze",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    async def _rejection_notified() -> bool:
        new_msgs = harness.channel.messages[prev_notifications:]
        return any(
            m.conversation_id == seed.conversation_id
            and "rejected" in m.text.lower()
            and "not this quarter" in m.text
            for m in new_msgs
        )

    await harness.wait_for(_rejection_notified, timeout=5.0)

    # Give the Worker a generous moment to (incorrectly) hit MCP if it
    # were going to.
    await asyncio.sleep(0.5)
    assert harness.mcp.received == [], (
        "rejected requests must never trigger an MCP call"
    )

    audit_actions = [
        e.action for e in await pg.list_approval_audit(req.id, tenant_id=seed.tenant_id)
    ]
    assert audit_actions == ["created", "rejected"], (
        f"audit chain mismatch: {audit_actions!r}"
    )
    rejected_entry = [
        e for e in await pg.list_approval_audit(req.id, tenant_id=seed.tenant_id) if e.action == "rejected"
    ][0]
    assert rejected_entry.actor_user_id == seed.owner_user_id
    assert rejected_entry.note is not None
    assert "not this quarter" in rejected_entry.note
