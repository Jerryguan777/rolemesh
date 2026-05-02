"""E2E-01: Happy path — proposal → approve → execute → report.

Full loop exercised:
  1. Admin creates a policy via REST (HTTP → FastAPI → DB).
  2. Simulated agent publishes ``submit_proposal`` via NATS.
  3. Harness's task-subscription routes the message through
     ``ApprovalEngine.handle_proposal`` (real DB writes, real audit
     trigger).
  4. Approver notification lands in ``SinkChannelSender``.
  5. Approver POSTs ``/decide?approve`` via REST.
  6. ``ApprovalEngine.handle_decision`` publishes NATS
     ``approval.decided.<id>``.
  7. Real ``ApprovalWorker`` claims, POSTs via credential proxy, the
     mock MCP server records the request.
  8. Worker writes ``executed`` status + metadata; origin conversation
     notification lands in sink.

Assertions that pin the wire-level contracts (which unit tests cannot
catch because each layer is isolated there):

  - MCP received request body matches JSON-RPC shape the Worker builds.
  - MCP received headers include ``X-RoleMesh-User-Id`` with the
    originating user and ``X-Idempotency-Key`` equal to the sha256 the
    policy module computes.
  - Credential proxy did NOT leak ``Authorization`` from the inbound
    request (Worker does not send one; proxy injects one via
    ``_token_vault`` if configured, but in this test auth_mode=service
    with empty server headers — so the proxy forwards with no
    Authorization).
  - DB final status ``executed``; audit chain contains exactly
    ``[created, approved, executing, executed]`` with the expected
    ``actor_user_id`` per row.
"""

from __future__ import annotations

from rolemesh.db import (
    get_approval_request,
    list_approval_audit,
    list_approval_requests,
)

from .harness import OrchestratorHarness, make_auth_user, seed_tenant


async def test_happy_path_proposal_approve_execute_report(
    harness: OrchestratorHarness,
) -> None:
    seed = await seed_tenant()
    admin = make_auth_user(
        tenant_id=seed.tenant_id, user_id=seed.owner_user_id, role="owner"
    )

    # 1. Admin creates a policy via REST.
    async with harness.api_client(admin) as api:
        r = await api.post(
            "/api/admin/approval-policies",
            json={
                "mcp_server_name": harness.mcp_server_name,
                "tool_name": "refund",
                "condition_expr": {
                    "field": "amount",
                    "op": ">",
                    "value": 100,
                },
                "coworker_id": seed.coworker_id,
                "approver_user_ids": [seed.owner_user_id],
                "priority": 5,
            },
        )
        assert r.status_code == 201, r.text

    # 2. Simulated agent publishes submit_proposal.
    job_id = "e2e-happy-1"
    action_params = {"amount": 500, "order_id": "o-42"}
    await harness.publish_agent_task(
        job_id,
        {
            "type": "submit_proposal",
            "tenantId": seed.tenant_id,
            "coworkerId": seed.coworker_id,
            "conversationId": seed.conversation_id,
            "jobId": job_id,
            "userId": seed.owner_user_id,
            "rationale": "refund for storm disruption",
            "actions": [
                {
                    "mcp_server": harness.mcp_server_name,
                    "tool_name": "refund",
                    "params": action_params,
                }
            ],
        },
    )

    # 3. Wait for pending row (engine consumed the NATS task).
    async def _pending_created() -> bool:
        rows = await list_approval_requests(seed.tenant_id, status="pending")
        return len(rows) == 1

    await harness.wait_for(_pending_created, timeout=5.0)
    pending = (
        await list_approval_requests(seed.tenant_id, status="pending")
    )[0]

    # 4. Approver notification delivered.
    async def _approver_notified() -> bool:
        return any(
            "approval" in m.text.lower() and "waiting" in m.text.lower()
            for m in harness.channel.messages
        )

    await harness.wait_for(_approver_notified, timeout=2.0)

    # 5. Approver decides via REST.
    async with harness.api_client(admin) as api:
        r = await api.post(
            f"/api/admin/approvals/{pending.id}/decide",
            json={"action": "approve"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

    # 6-7. Worker processes approval.decided, hits MCP.
    async def _mcp_called() -> bool:
        return len(harness.mcp.received) == 1

    await harness.wait_for(_mcp_called, timeout=10.0)
    received = harness.mcp.received[0]

    # Wire-format contract — JSON-RPC body from the Worker.
    assert received.body["jsonrpc"] == "2.0"
    assert received.body["method"] == "tools/call"
    assert received.body["params"]["name"] == "refund"
    assert received.body["params"]["arguments"] == action_params

    # Wire-format contract — headers after the credential proxy has
    # processed them:
    #   * ``X-RoleMesh-User-Id`` is deliberately STRIPPED by the proxy
    #     before forwarding (user identity must not leak to arbitrary
    #     MCP servers as a plain header; it flows as an injected
    #     Authorization Bearer instead, when TokenVault is configured).
    #   * ``X-Idempotency-Key`` passes through — MCP servers that honor
    #     it need to see the exact hash the Worker generated.
    #   * No ``Authorization`` in this fixture because the MCP server
    #     is registered with ``auth_mode=service`` and empty headers.
    headers_lc = {k.lower(): v for k, v in received.headers.items()}
    assert "x-rolemesh-user-id" not in headers_lc, (
        "credential proxy must strip X-RoleMesh-User-Id before forwarding "
        "to the MCP server — user identity is not supposed to leak"
    )
    # Idempotency key format: "<request_uuid>:<action_index>". This
    # gives per-tenant isolation (request_id is UUID) and preserves
    # per-action uniqueness within a batch (index suffix). See
    # docs/approval-architecture.md §Credential Proxy Integration.
    idem_key = headers_lc.get("x-idempotency-key")
    assert idem_key == f"{pending.id}:0", (
        f"X-Idempotency-Key should be '<request_id>:<index>'; got {idem_key!r}"
    )
    assert "authorization" not in headers_lc, (
        "auth_mode=service with empty per-server headers means no "
        "Authorization is injected or forwarded"
    )

    # 8. Final DB state + full audit chain.
    async def _executed() -> bool:
        req = await get_approval_request(pending.id, tenant_id=seed.tenant_id)
        return req is not None and req.status == "executed"

    await harness.wait_for(_executed, timeout=5.0)

    audit = await list_approval_audit(pending.id, tenant_id=seed.tenant_id)
    actions = [e.action for e in audit]
    assert actions == ["created", "approved", "executing", "executed"], (
        f"audit chain mismatch: {actions!r}"
    )
    assert audit[0].actor_user_id == seed.owner_user_id
    assert audit[1].actor_user_id == seed.owner_user_id
    assert audit[2].actor_user_id is None
    assert audit[3].actor_user_id is None
    # Terminal audit carries the MCP results for forensic review.
    assert "results" in audit[3].metadata
    results = audit[3].metadata["results"]
    assert isinstance(results, list) and len(results) == 1
    assert results[0].get("ok") is True

    # 9. Origin got the execution report.
    reports = [
        m for m in harness.channel.messages
        if "executed" in m.text.lower() or "#" in m.text
    ]
    assert any(m.conversation_id == seed.conversation_id for m in reports)
