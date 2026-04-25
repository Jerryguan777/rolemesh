"""E2E-03: MCP server returns HTTP 200 with a JSON-RPC ``error`` body.

This is the canonical way an MCP server signals "I received your call
but it was wrong / the tool failed" (JSON-RPC 2.0 §5.1). It is NOT an
HTTP error; the transport succeeded. The application-level error lives
in the body.

Review hypothesis:
    ApprovalWorker treats ``HTTP status < 400`` as success and records
    the body under ``results[i]["response"]``. That means a failed
    MCP call is recorded as ``{"ok": True, "response": {..., "error":
    {...}}}`` and the batch terminates as ``executed`` instead of
    ``execution_failed``. The per-action error is invisible to the
    admin UI unless they hand-read the metadata JSON.

What a correct Worker should do:
    Inspect the JSON-RPC body; if ``error`` is set AND ``result`` is
    absent, record the action as an error. The batch rolls up to
    ``execution_failed``.

Outcome of this test:
    Should FAIL against current main. Documenting the bug here so
    anyone running the suite sees it immediately. The follow-up fix
    (Task #38) adjusts executor.py to honor JSON-RPC error semantics.
"""

from __future__ import annotations

from rolemesh.db import pg

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_mcp_jsonrpc_error_is_recorded_as_failure(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    admin = make_auth_user(
        tenant_id=seed.tenant_id, user_id=seed.owner_user_id
    )

    # Policy that matches this tool regardless of amount so we never
    # hit the "no-match → auto-executed" path.
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

    # Queue a JSON-RPC error response. HTTP 200 with no "result"
    # and a populated "error" object is the JSON-RPC 2.0 contract for
    # a tool invocation that failed at the MCP server side.
    harness.mcp.enqueue(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32602,
                "message": "Invalid params: amount must be positive",
            },
        }
    )

    job_id = "e2e-app-error"
    await harness.publish_agent_task(
        job_id,
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": job_id,
            "userId": seed.owner_user_id,
            "rationale": "refund",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": {"amount": -5},
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

    async with harness.api_client(admin) as api:
        r = await api.post(
            f"/api/admin/approvals/{req.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 200

    # Wait for worker to hit the MCP mock (it will exactly once).
    async def _mcp_hit() -> bool:
        return len(harness.mcp.received) == 1

    await harness.wait_for(_mcp_hit, timeout=10.0)

    # Wait for the Worker to write the terminal status. Whatever it is,
    # we will now assert the correct one.
    async def _terminal() -> bool:
        fresh = await pg.get_approval_request(req.id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status in (
            "executed",
            "execution_failed",
        )

    await harness.wait_for(_terminal, timeout=5.0)
    fresh = await pg.get_approval_request(req.id, tenant_id=seed.tenant_id)
    assert fresh is not None

    # THE ASSERTION: a JSON-RPC application error MUST mark the batch
    # as execution_failed. Anything else means the admin sees a green
    # status on a call that actually failed upstream.
    assert fresh.status == "execution_failed", (
        "MCP server returned HTTP 200 with JSON-RPC 'error' and no 'result'. "
        "The Worker must treat this as a per-action failure — it is the "
        "canonical way an MCP server signals an error. Instead the "
        f"Worker recorded status={fresh.status!r}, hiding the failure."
    )

    # And the audit metadata should record the JSON-RPC error text so an
    # operator diagnosing the request can see what went wrong.
    audit = await pg.list_approval_audit(req.id, tenant_id=seed.tenant_id)
    terminal_entry = [e for e in audit if e.action == "execution_failed"]
    assert terminal_entry, "audit should include an execution_failed row"
    results = terminal_entry[0].metadata.get("results") or []
    assert results, "metadata.results must be populated for forensic review"
    first = results[0]
    assert "error" in first, (
        "per-action result must flag the error so the admin UI can surface "
        "it: got %r" % first
    )
    assert "Invalid params" in str(first["error"]), (
        "error string should preserve the JSON-RPC message so the "
        "operator can diagnose without reading metadata JSON"
    )


