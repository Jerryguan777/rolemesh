"""E2E: tenant-level behaviour when a proposal matches no policy.

The tenant's ``approval_default_mode`` column steers the fallback:

  * ``auto_execute`` (default, legacy) — executes unsupervised.
  * ``require_approval`` — create as skipped; nobody runs the actions
    unless an admin adds a policy and retries.
  * ``deny`` — create as rejected with a system note; origin gets a
    rejection message.

These are covered separately because tmp.txt audit flagged the
"auto_execute after admin deletes a policy" path as a real security
window. Operators who want strictness can pick either of the latter
two modes.
"""

from __future__ import annotations

import asyncio

from rolemesh.db import (
    get_approval_request,
    list_approval_audit,
    list_approval_requests,
    update_tenant,
)

from .harness import OrchestratorHarness, seed_tenant


async def _submit_unmatched_proposal(
    harness: OrchestratorHarness,
    seed,
) -> str:
    """Helper: publish a proposal that matches no enabled policy in
    the tenant. Waits for DB row and returns its id."""
    await harness.publish_agent_task(
        f"e2e-nomatch-{seed.tenant_id[:8]}",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": f"e2e-nomatch-{seed.tenant_id[:8]}",
            "userId": seed.owner_user_id,
            "rationale": "no-match scenario",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "whatever",
                    "params": {"amount": 1},
                }
            ],
        },
    )

    async def _row_exists() -> bool:
        return bool(await list_approval_requests(seed.tenant_id))

    await harness.wait_for(_row_exists, timeout=5.0)
    return (await list_approval_requests(seed.tenant_id))[0].id


async def test_auto_execute_mode_runs_unsupervised(
    harness: OrchestratorHarness,
) -> None:
    """Legacy behaviour — preserved for existing deployments."""
    seed = await seed_tenant()
    # Default is 'auto_execute'; no explicit update required.
    req_id = await _submit_unmatched_proposal(harness, seed)

    async def _executed() -> bool:
        fresh = await get_approval_request(req_id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "executed"

    await harness.wait_for(_executed, timeout=10.0)
    assert len(harness.mcp.received) == 1


async def test_require_approval_mode_blocks_unsupervised_execution(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    await update_tenant(
        seed.tenant_id, approval_default_mode="require_approval"
    )
    req_id = await _submit_unmatched_proposal(harness, seed)

    async def _skipped() -> bool:
        fresh = await get_approval_request(req_id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "skipped"

    await harness.wait_for(_skipped, timeout=5.0)
    # MCP must never be called.
    await asyncio.sleep(0.3)
    assert harness.mcp.received == [], (
        "require_approval mode must not trigger any MCP execution"
    )
    # Origin gets a skipped notification (reuses the existing skipped
    # message since the user-visible outcome is identical).
    assert any(
        m.conversation_id == seed.conversation_id
        and (
            "not proceed" in m.text.lower()
            or "no approver" in m.text.lower()
        )
        for m in harness.channel.messages
    )


async def test_deny_mode_rejects_with_system_note(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    await update_tenant(
        seed.tenant_id, approval_default_mode="deny"
    )
    req_id = await _submit_unmatched_proposal(harness, seed)

    async def _rejected() -> bool:
        fresh = await get_approval_request(req_id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "rejected"

    await harness.wait_for(_rejected, timeout=5.0)
    await asyncio.sleep(0.3)
    assert harness.mcp.received == [], (
        "deny mode must not trigger any MCP execution"
    )
    # The rejection audit row carries the system note explaining why.
    audit = [
        e for e in await list_approval_audit(req_id, tenant_id=seed.tenant_id)
        if e.action == "rejected"
    ]
    assert audit, "audit must include a rejected row"
    assert audit[0].actor_user_id is None, (
        "system-initiated rejection must have NULL actor"
    )
    assert audit[0].note is not None
    assert "deny-by-default" in audit[0].note.lower()
    # Origin gets a rejection-style message (delivered via the Worker
    # handling the decided NATS event).
    await harness.wait_for(
        lambda: _async_true(
            any(
                m.conversation_id == seed.conversation_id
                and "rejected" in m.text.lower()
                for m in harness.channel.messages
            )
        ),
        timeout=5.0,
    )


async def _async_true(v: bool) -> bool:
    return v
