"""DB-layer cross-tenant isolation regression tests.

Pre-commit fc4b0... `get_approval_policy(policy_id)`,
`get_approval_request(request_id)`, `get_safety_rule(rule_id)`, and
`list_approval_audit(request_id)` looked up rows by id alone, with no
tenant filter. Tenant isolation was enforced one layer up (REST handlers
checked `row.tenant_id == user.tenant_id`).

If any caller forgot the upstream check — or a future caller forgot the
upstream check — the DB would happily hand over another tenant's data.
These tests pin the boundary at the DB function layer: each lookup MUST
reject mismatched tenant_id.

`approval_audit_log` previously had no `tenant_id` column at all
(rows were tied to `approval_requests` only via FK), so cross-tenant
audit reads relied entirely on the upstream `get_approval_request`
check. The audit-log test below verifies the DB function now refuses
mismatched tenants on its own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rolemesh.db.pg import (
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_safety_rule,
    create_tenant,
    create_user,
    get_approval_policy,
    get_approval_request,
    get_safety_rule,
    list_approval_audit,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Two-tenant fixture
# ---------------------------------------------------------------------------


async def _two_tenants() -> dict[str, dict[str, str]]:
    """Build two complete (tenant, user, coworker, conversation) chains."""
    out: dict[str, dict[str, str]] = {}
    for tag in ("A", "B"):
        t = await create_tenant(name=f"Tenant {tag}", slug=f"t-{tag.lower()}-{uuid.uuid4().hex[:6]}")
        u = await create_user(
            tenant_id=t.id,
            name=f"User {tag}",
            email=f"u-{tag.lower()}-{uuid.uuid4().hex[:6]}@x.com",
            role="owner",
        )
        cw = await create_coworker(
            tenant_id=t.id, name=f"CW{tag}", folder=f"cw-{tag.lower()}-{uuid.uuid4().hex[:6]}"
        )
        b = await create_channel_binding(
            coworker_id=cw.id,
            tenant_id=t.id,
            channel_type="telegram",
            credentials={"bot_token": "x"},
        )
        conv = await create_conversation(
            tenant_id=t.id,
            coworker_id=cw.id,
            channel_binding_id=b.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        out[tag] = {
            "tenant_id": t.id,
            "user_id": u.id,
            "coworker_id": cw.id,
            "conversation_id": conv.id,
        }
    return out


# ---------------------------------------------------------------------------
# get_approval_policy
# ---------------------------------------------------------------------------


async def test_get_approval_policy_rejects_mismatched_tenant() -> None:
    """A policy created for tenant A must NOT be returned when looked
    up under tenant B. Defense: function signature requires tenant_id
    and SQL filters on it."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    policy = await create_approval_policy(
        tenant_id=a["tenant_id"],
        coworker_id=a["coworker_id"],
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
    )
    # Same-tenant lookup works.
    same_tenant = await get_approval_policy(policy.id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == policy.id

    # Cross-tenant lookup must return None — no leakage even with
    # a valid policy_id.
    leaked = await get_approval_policy(policy.id, tenant_id=b["tenant_id"])
    assert leaked is None, (
        "get_approval_policy returned tenant A's policy when called "
        "with tenant B's id — DB-layer isolation broken"
    )


# ---------------------------------------------------------------------------
# get_approval_request
# ---------------------------------------------------------------------------


async def test_get_approval_request_rejects_mismatched_tenant() -> None:
    """An approval request created for tenant A must NOT be returned
    when looked up under tenant B."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    req = await create_approval_request(
        tenant_id=a["tenant_id"],
        coworker_id=a["coworker_id"],
        conversation_id=a["conversation_id"],
        policy_id=None,
        user_id=a["user_id"],
        job_id=f"j-{uuid.uuid4().hex[:8]}",
        mcp_server_name="erp",
        actions=[{"tool_name": "refund", "params": {}}],
        action_hashes=[uuid.uuid4().hex],
        rationale="test",
        source="proposal",
        status="pending",
        resolved_approvers=[a["user_id"]],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    same_tenant = await get_approval_request(req.id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == req.id

    leaked = await get_approval_request(req.id, tenant_id=b["tenant_id"])
    assert leaked is None, (
        "get_approval_request leaked tenant A's request to tenant B"
    )


# ---------------------------------------------------------------------------
# get_safety_rule
# ---------------------------------------------------------------------------


async def test_get_safety_rule_rejects_mismatched_tenant() -> None:
    """A safety rule created for tenant A must NOT be returned when
    looked up under tenant B."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    rule = await create_safety_rule(
        tenant_id=a["tenant_id"],
        stage="pre_tool_call",
        check_id="domain_allowlist",
        config={"allowed_hosts": ["api.example.com"]},
    )

    same_tenant = await get_safety_rule(rule.id, tenant_id=a["tenant_id"])
    assert same_tenant is not None
    assert same_tenant.id == rule.id

    leaked = await get_safety_rule(rule.id, tenant_id=b["tenant_id"])
    assert leaked is None, (
        "get_safety_rule leaked tenant A's rule to tenant B"
    )


# ---------------------------------------------------------------------------
# list_approval_audit
# ---------------------------------------------------------------------------


async def test_list_approval_audit_rejects_mismatched_tenant() -> None:
    """The audit-trigger writes a 'created' row whenever an approval
    request is INSERTed. Reading those audit rows by request_id alone
    used to leak across tenants because the audit table had no tenant
    column. Now the function takes tenant_id and filters on it."""
    tenants = await _two_tenants()
    a, b = tenants["A"], tenants["B"]

    req = await create_approval_request(
        tenant_id=a["tenant_id"],
        coworker_id=a["coworker_id"],
        conversation_id=a["conversation_id"],
        policy_id=None,
        user_id=a["user_id"],
        job_id=f"j-{uuid.uuid4().hex[:8]}",
        mcp_server_name="erp",
        actions=[{"tool_name": "refund", "params": {}}],
        action_hashes=[uuid.uuid4().hex],
        rationale="test",
        source="proposal",
        status="pending",
        resolved_approvers=[a["user_id"]],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    # Same-tenant read works and returns the trigger-written 'created' row.
    same_tenant = await list_approval_audit(req.id, tenant_id=a["tenant_id"])
    assert same_tenant, "same-tenant audit read returned nothing"
    assert any(e.action == "created" for e in same_tenant)

    # Cross-tenant read returns empty even with a valid request_id.
    leaked = await list_approval_audit(req.id, tenant_id=b["tenant_id"])
    assert leaked == [], (
        "list_approval_audit leaked audit rows for tenant A's request "
        "to tenant B — audit table tenant filter broken"
    )
