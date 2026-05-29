"""E2E: Case A (no matching policy) — v6.1 collapsed auto-allow path.

v6.1 §P2.3 removed ``tenants.approval_default_mode``. When a proposal
does not match any enabled policy, the engine now always:

  1. inserts an ``approval_requests`` row stamped ``source='auto_execute'``,
  2. flips its status to ``approved``,
  3. publishes ``decided`` so the Worker executes the actions,
  4. leaves an audit trail (created → approved → executing → executed)
     so operators can later distinguish system-allowed from
     human-approved runs by inspecting the ``source`` column.

Why no per-mode tests anymore: the legacy ``require_approval`` and
``deny`` modes were deleted along with the column (see
``docs/design/auth-approval-v6.md`` §P2.3 for the rationale). Future
default-deny posture will return via a ``policy.action='allow'``
primitive paired with tenant-level allow-list — not a free-standing
escape hatch.
"""

from __future__ import annotations

from rolemesh.db import (
    get_approval_request,
    list_approval_audit,
    list_approval_requests,
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


async def test_no_match_auto_allows_and_records_source_auto_execute(
    harness: OrchestratorHarness,
) -> None:
    """T2a.3 — Case A: no policy matches → row carries
    source='auto_execute' (not 'proposal'), status reaches 'executed',
    MCP was called once. The audit trail must show 'approved' with
    NULL actor (system-initiated, not a forged human decision)."""
    seed = await seed_tenant()
    req_id = await _submit_unmatched_proposal(harness, seed)

    async def _executed() -> bool:
        fresh = await get_approval_request(req_id, tenant_id=seed.tenant_id)
        return fresh is not None and fresh.status == "executed"

    await harness.wait_for(_executed, timeout=10.0)
    fresh = await get_approval_request(req_id, tenant_id=seed.tenant_id)
    assert fresh is not None

    # The whole point of the v6.1 rewrite: the row's source records the
    # entry path. 'auto_execute' here proves we did not impersonate the
    # 'proposal' source (which now means "policy matched + a human
    # took a decision path"). Mutating this string in engine.py to
    # 'proposal' must fail this test.
    assert fresh.source == "auto_execute", (
        f"Case A row must record source='auto_execute' so audits can "
        f"distinguish system-allowed from policy-matched proposals; "
        f"got {fresh.source!r}"
    )
    assert len(harness.mcp.received) == 1, (
        "auto_execute path must run the MCP call exactly once"
    )

    # The 'approved' audit row must have NULL actor — the engine
    # auto-allowed without any human decision. A non-null actor would
    # be a forgery of human approval into the audit history.
    audit = await list_approval_audit(req_id, tenant_id=seed.tenant_id)
    approved_rows = [a for a in audit if a.action == "approved"]
    assert approved_rows, "audit trail must include an 'approved' row"
    assert approved_rows[0].actor_user_id is None, (
        "system-initiated approval must record NULL actor"
    )
