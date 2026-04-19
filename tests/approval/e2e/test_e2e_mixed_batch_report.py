"""E2E-14: Mixed success/failure batch report is operator-readable.

A 2-action batch where:
  - action[0] succeeds (HTTP 200 + valid JSON-RPC result)
  - action[1] returns HTTP 500

The Worker must:
  - Execute BOTH actions (best-effort batch; one failure does not
    short-circuit the rest).
  - Mark the batch as ``execution_failed``.
  - Send a report to the origin that contains BOTH a success line and
    a failure line, with the HTTP error text present enough to be
    diagnosable.

We use HTTP 500 (not JSON-RPC error) because E2E-03 already covers the
application-error case; this one is about network/transport-level
failures, which ARE correctly handled today. If both E2E-03 and this
test fail, it proves two distinct bugs.
"""

from __future__ import annotations

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_mixed_batch_produces_readable_report(
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
                "tool_name": "*",
                "condition_expr": {"always": True},
                "coworker_id": seed.coworker_id,
                "approver_user_ids": [seed.owner_user_id],
            },
        )

    # First action → default success response. Second → forced 500.
    # Queue defaults: only the SECOND response is a failure.
    harness.mcp.enqueue({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "refund ok"}]},
    })
    harness.mcp.enqueue((500, "upstream unavailable"))

    await harness.publish_agent_task(
        "e2e-mixed",
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": "e2e-mixed",
            "userId": seed.owner_user_id,
            "rationale": "batch mixed",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": 1, "tag": "a"},
                },
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "cancel_order",
                    "params": {"order_id": "o-b"},
                },
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

    async with harness.api_client(admin) as api:
        r = await api.post(
            f"/api/admin/approvals/{req.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 200

    async def _both_called() -> bool:
        return len(harness.mcp.received) == 2

    await harness.wait_for(_both_called, timeout=10.0)

    async def _terminal() -> bool:
        fresh = await pg.get_approval_request(req.id)
        return fresh is not None and fresh.status in (
            "executed",
            "execution_failed",
        )

    await harness.wait_for(_terminal, timeout=5.0)
    fresh = await pg.get_approval_request(req.id)
    assert fresh is not None
    assert fresh.status == "execution_failed", (
        "one HTTP 500 among the actions must flip the batch to "
        "execution_failed"
    )

    # The terminal audit row carries per-action results.
    audit = [
        e for e in await pg.list_approval_audit(req.id)
        if e.action == "execution_failed"
    ]
    assert audit, "audit must contain execution_failed row"
    results = audit[0].metadata.get("results") or []
    assert len(results) == 2
    assert results[0].get("ok") is True
    assert "error" in results[1]
    assert "500" in str(results[1]["error"]) or "MCP 500" in str(results[1]["error"])

    # Origin report includes BOTH a success marker and a failure
    # marker. (Exact text is part of notification.format_execution_report.)
    report_msgs = [
        m for m in harness.channel.messages
        if m.conversation_id == seed.conversation_id
        and "#" in m.text  # includes request id shortcode
    ]
    assert report_msgs, "origin must get an execution report"
    report_text = report_msgs[-1].text
    assert "[ok]" in report_text, (
        "successful action should render with an ok marker"
    )
    assert "[x]" in report_text, (
        "failed action should render with an error marker"
    )
