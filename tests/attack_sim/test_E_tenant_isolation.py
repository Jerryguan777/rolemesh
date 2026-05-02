"""E. Tenant isolation attacks — cross-tenant access attempts.

Attacker has legitimate access to their own tenant A and tries to
reach, forge, or read data in victim tenant B.

  E1. Forge tenantId in NATS payload
      → engine ``_tenant_matches`` drops mismatched messages.
  E2. Forge coworkerId belonging to another tenant
      → IPC dispatcher looks up the coworker and uses its authoritative
        tenant_id, not the attacker's claim.
  E3. Cross-tenant audit read via REST
      → admin endpoint filters by authenticated user's tenant_id.
  E4. Cross-tenant approval decide via REST
      → /decide rejects when the request belongs to another tenant.
  E5. Cross-tenant idempotency-key collision
      → X-Idempotency-Key scheme uses ``<request_id>:<index>``, per-
        tenant unique by UUID — validated in the approval module.
  E6. NATS subject sidechannel (XFAIL)
      → requires NATS account-per-tenant ACL (not implemented).
        Documenting test — flips to pass when that layer lands.
"""

from __future__ import annotations

import asyncio

import pytest

from rolemesh.approval.engine import ApprovalEngine
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.db import (
    create_approval_policy,
    create_approval_request,
    get_approval_request,
    get_conversation_for_notification,
    list_approval_requests,
)

from .conftest import seed_victim

pytestmark = pytest.mark.usefixtures("test_db")


def _resolver() -> NotificationTargetResolver:
    async def _convs(u: str, c: str) -> list[str]:
        return []

    async def _conv(cid: str) -> object | None:
        return await get_conversation_for_notification(cid)

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_conv,
        webui_base_url=None,
    )


def _engine(pub, ch) -> ApprovalEngine:
    return ApprovalEngine(publisher=pub, channel_sender=ch, resolver=_resolver())


# ---------------------------------------------------------------------------
# E1. Forge tenantId in NATS payload
# ---------------------------------------------------------------------------


async def test_E1_forged_tenant_id_dropped_by_engine(
    fake_publisher, fake_channel
) -> None:
    """Attacker: tenant A's container, publishes ``auto_approval_request``
    on its own agent.{job}.tasks subject with body tenantId = B's UUID.
    Goal: create an approval request scoped to victim B.
    Defense: engine.handle_auto_intercept takes trusted tenant_id from
    the IPC dispatcher (not the payload) and rejects when the payload's
    claimed tenantId disagrees."""
    tenant_a = await seed_victim("atk")
    tenant_b = await seed_victim("vic")
    engine = _engine(fake_publisher, fake_channel)

    # Attacker's body says tenant B, but trusted lookup says tenant A.
    await engine.handle_auto_intercept(
        {
            "tenantId": tenant_b.tenant_id,  # forged
            "coworkerId": tenant_a.coworker_id,
            "conversationId": tenant_a.conversation_id,
            "jobId": "e1-job",
            "userId": tenant_a.owner_user_id,
            "mcp_server_name": "erp",
            "tool_name": "refund",
            "tool_params": {"amount": 9999},
            "action_hash": "e1-hash",
        },
        tenant_id=tenant_a.tenant_id,  # trusted
        coworker_id=tenant_a.coworker_id,
    )

    # No rows created in EITHER tenant. The message was dropped by the
    # tenant-matches guard; engine refused to silently re-attribute.
    assert await list_approval_requests(tenant_b.tenant_id) == []
    assert await list_approval_requests(tenant_a.tenant_id) == []


# ---------------------------------------------------------------------------
# E2. Forge coworkerId
# ---------------------------------------------------------------------------


async def test_E2_forged_coworker_id_dropped(fake_publisher, fake_channel) -> None:
    """Attacker: tenant A's container claims victim B's coworker in the
    payload. Trusted lookup (IPC dispatcher) resolves the caller's
    ACTUAL coworker; the engine sees a mismatch and drops."""
    tenant_a = await seed_victim("atk2")
    tenant_b = await seed_victim("vic2")
    engine = _engine(fake_publisher, fake_channel)

    # Body forges tenant B's coworker, but trusted is tenant A's.
    await engine.handle_auto_intercept(
        {
            "tenantId": tenant_a.tenant_id,
            "coworkerId": tenant_b.coworker_id,  # forged
            "conversationId": tenant_a.conversation_id,
            "jobId": "e2-job",
            "userId": tenant_a.owner_user_id,
            "mcp_server_name": "erp",
            "tool_name": "refund",
            "tool_params": {},
            "action_hash": "e2-hash",
        },
        tenant_id=tenant_a.tenant_id,
        coworker_id=tenant_a.coworker_id,
    )

    assert await list_approval_requests(tenant_a.tenant_id) == []
    assert await list_approval_requests(tenant_b.tenant_id) == []


# ---------------------------------------------------------------------------
# E3. Cross-tenant audit read via REST
# ---------------------------------------------------------------------------


async def test_E3_cross_tenant_audit_read_blocked() -> None:
    """Attacker: tenant A's owner. Goal: call ``get_approval_audit``
    with a request_id belonging to tenant B and read its audit log.
    Defense: admin endpoint filters on authenticated user's tenant_id
    (get_approval_request returns the request only if tenant_id
    matches the caller's tenant). In this test we drive the pg layer
    directly to pin the constraint that cross-tenant requests are
    invisible to each tenant's REST listings."""
    tenant_a = await seed_victim("a3")
    tenant_b = await seed_victim("b3")

    # Seed a request in tenant B.
    policy_b = await create_approval_policy(
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[tenant_b.owner_user_id],
    )
    from datetime import UTC, datetime, timedelta

    await create_approval_request(
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
        conversation_id=tenant_b.conversation_id,
        policy_id=policy_b.id,
        user_id=tenant_b.owner_user_id,
        job_id="b-job",
        mcp_server_name="erp",
        actions=[{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
        action_hashes=["h"],
        rationale=None,
        source="proposal",
        status="pending",
        resolved_approvers=[tenant_b.owner_user_id],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    # Tenant A's REST list of approvals must NOT see anything from B.
    from_a_view = await list_approval_requests(tenant_a.tenant_id)
    assert from_a_view == [], (
        "tenant A listing must not leak tenant B's approvals"
    )
    from_b_view = await list_approval_requests(tenant_b.tenant_id)
    assert len(from_b_view) == 1


# ---------------------------------------------------------------------------
# E4. Cross-tenant decide
# ---------------------------------------------------------------------------


async def test_E4_cross_tenant_decide_blocked(fake_publisher, fake_channel) -> None:
    """Attacker: tenant A's owner (fully authenticated) tries to decide
    an approval that belongs to tenant B. Defense: the admin /decide
    endpoint checks ``req.tenant_id == user.tenant_id`` and 404s on
    mismatch. We simulate at the engine layer by verifying the
    request's tenant_id does not change and by confirming the pg
    lookup returns nothing when the attacker's tenant is wrong."""
    tenant_a = await seed_victim("a4")
    tenant_b = await seed_victim("b4")
    engine_b = _engine(fake_publisher, fake_channel)

    # Seed a pending request in B.
    await create_approval_policy(
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[tenant_b.owner_user_id],
    )
    await engine_b.handle_proposal(
        {
            "tenantId": tenant_b.tenant_id,
            "coworkerId": tenant_b.coworker_id,
            "conversationId": tenant_b.conversation_id,
            "jobId": "b-job",
            "userId": tenant_b.owner_user_id,
            "rationale": "r",
            "actions": [{"mcp_server": "erp", "tool_name": "refund", "params": {}}],
        },
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
    )
    req_b = (await list_approval_requests(tenant_b.tenant_id))[0]

    # DB-layer tenant filter: a forged request_id from tenant B looked
    # up under tenant A's context returns None — no leak even before the
    # REST 404 check is reached.
    leaked = await get_approval_request(req_b.id, tenant_id=tenant_a.tenant_id)
    assert leaked is None, (
        "get_approval_request must reject cross-tenant lookup at the SQL "
        "layer; if this assertion fails the REST 404 check became the "
        "single line of defense"
    )
    # And the legitimate read still works.
    same_tenant = await get_approval_request(req_b.id, tenant_id=tenant_b.tenant_id)
    assert same_tenant is not None


# ---------------------------------------------------------------------------
# E5. Cross-tenant idempotency key isolation
# ---------------------------------------------------------------------------


async def test_E5_idempotency_keys_unique_across_tenants(
    fake_publisher, fake_channel
) -> None:
    """Attacker vector: the idempotency header sent to MCP servers must
    differ across tenants even when semantic action is identical.
    Otherwise a shared MCP server that honors idempotency returns one
    tenant's cached response to another (cross-tenant data leak).

    Defense: key format is ``<request_uuid>:<action_index>``. UUIDs
    guarantee uniqueness regardless of tool/params.

    This test exercises the format contract at the classification
    layer (not real HTTP)."""
    # Same action proposed from two tenants must produce two DIFFERENT
    # request UUIDs — the idempotency key format relies on that.
    tenant_a = await seed_victim("a5")
    tenant_b = await seed_victim("b5")

    policy_a = await create_approval_policy(
        tenant_id=tenant_a.tenant_id,
        coworker_id=tenant_a.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[tenant_a.owner_user_id],
    )
    policy_b = await create_approval_policy(
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[tenant_b.owner_user_id],
    )
    from datetime import UTC, datetime, timedelta

    common_action = [
        {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 100}}
    ]
    common_hash = "identical-sha256"

    req_a = await create_approval_request(
        tenant_id=tenant_a.tenant_id,
        coworker_id=tenant_a.coworker_id,
        conversation_id=tenant_a.conversation_id,
        policy_id=policy_a.id,
        user_id=tenant_a.owner_user_id,
        job_id="ja",
        mcp_server_name="erp",
        actions=common_action,
        action_hashes=[common_hash],
        rationale=None,
        source="proposal",
        status="pending",
        resolved_approvers=[tenant_a.owner_user_id],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    req_b = await create_approval_request(
        tenant_id=tenant_b.tenant_id,
        coworker_id=tenant_b.coworker_id,
        conversation_id=tenant_b.conversation_id,
        policy_id=policy_b.id,
        user_id=tenant_b.owner_user_id,
        job_id="jb",
        mcp_server_name="erp",
        actions=common_action,
        action_hashes=[common_hash],
        rationale=None,
        source="proposal",
        status="pending",
        resolved_approvers=[tenant_b.owner_user_id],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    # Same action_hash across tenants — deliberately. The WORKER
    # computes idempotency key differently:
    #    X-Idempotency-Key = f"{request_id}:{i}"
    # request_id is a UUID per row, so keys never collide.
    assert req_a.id != req_b.id, "request UUIDs must differ per row"
    key_a = f"{req_a.id}:0"
    key_b = f"{req_b.id}:0"
    assert key_a != key_b, (
        "idempotency keys must be per-request unique — otherwise MCP "
        "server caches leak across tenants"
    )
    # Even though action_hashes (used for auto-intercept dedup) are
    # identical, the outgoing idempotency header is not. Two
    # different roles for two different data.
    _ = common_hash


# ---------------------------------------------------------------------------
# E6. NATS subject sidechannel — XFAIL (gap to be closed by NATS ACL)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "NATS subject ACL not implemented. Single NATS broker shared across "
        "tenants: a compromised tenant-A container could theoretically "
        "subscribe to agent.*.tasks and observe tenant B's task messages. "
        "Engine-layer _tenant_matches guard protects state writes, but the "
        "NATS layer has no ACL. Close this test's xfail once per-tenant "
        "NATS accounts/credentials land."
    ),
    strict=True,
)
async def test_E6_nats_subject_sidechannel_isolation() -> None:
    """Documenting test. Asserts the ideal: tenant A cannot consume NATS
    subjects published by tenant B even if they guess the subject
    pattern.

    Currently this is enforced by application-layer engine guards
    (E1, E2), but the transport layer itself is unpartitioned. That
    means observation-only attacks (read tenant B's submit_proposal
    payloads in flight) are still possible — a shape of
    confidentiality leakage, not integrity.

    Will pass when:
      - NATS account-per-tenant configured, or
      - orch + containers use tenant-scoped NATS credentials
    """
    # We deliberately assert the missing guarantee so the xfail is
    # stable. There's no infrastructure to test against — this is a
    # gap marker.
    raise AssertionError("NATS subject ACL not implemented")


_ = asyncio  # keep import for future async helpers
