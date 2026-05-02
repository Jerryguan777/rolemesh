"""PR-E: ``list_*`` admin paths return cross-tenant rows; downstream
must dispatch each row back into the per-tenant context.

The reconcile loops (approval expiry, stuck-approved republish,
stuck-executing salvage) all share this shape:

  1. Maintenance loop calls a ``list_*`` admin function — that
     function uses ``admin_conn()`` (BYPASSRLS) so it sees every
     tenant's pending rows in one scan.
  2. For each row, the handler dispatches back into a
     ``tenant_conn(row.tenant_id)`` block to actually mutate state.

That dispatch is what keeps RLS in play during the mutation step.
A regression like "loop reuses the admin connection for the UPDATE
too" would silently bypass RLS on the write path. This test pins
the contract by:

  - Inserting rows from two tenants.
  - Calling ``list_expired_pending_approvals`` (the canonical admin
    list) and asserting it returns rows from BOTH tenants.
  - Verifying each returned row carries its own ``tenant_id`` so
    the caller has the value it needs to switch contexts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rolemesh.db import (
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    list_expired_pending_approvals,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_expired_request(tag: str) -> tuple[str, str]:
    t = await create_tenant(name=f"T{tag}", slug=f"al-{tag.lower()}-{uuid.uuid4().hex[:6]}")
    u = await create_user(
        tenant_id=t.id, name=f"U{tag}",
        email=f"u-{tag.lower()}-{uuid.uuid4().hex[:6]}@x.com",
        role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name=f"CW{tag}",
        folder=f"cw-{tag.lower()}-{uuid.uuid4().hex[:6]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id,
        channel_type="telegram", credentials={"bot_token": "x"},
    )
    conv = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id, channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    # Backdate expires_at so the maintenance loop will see it as expired.
    expired_at = datetime.now(UTC) - timedelta(minutes=5)
    req = await create_approval_request(
        tenant_id=t.id, coworker_id=cw.id, conversation_id=conv.id,
        policy_id=None, user_id=u.id, job_id=f"j-{uuid.uuid4().hex[:8]}",
        mcp_server_name="erp",
        actions=[{"tool_name": "refund", "params": {}}],
        action_hashes=[uuid.uuid4().hex],
        rationale="t", source="proposal", status="pending",
        resolved_approvers=[u.id],
        expires_at=expired_at,
    )
    return t.id, req.id


async def test_list_expired_returns_all_tenants_with_their_tenant_ids() -> None:
    """A reconcile-style admin list must:

    1. See rows from every tenant (admin_conn bypass works).
    2. Carry tenant_id on each row so the dispatch step can hand
       it back to tenant_conn.
    """
    a_tenant, a_req = await _seed_expired_request("A")
    b_tenant, b_req = await _seed_expired_request("B")

    expired = await list_expired_pending_approvals()
    by_id = {r.id: r for r in expired}

    assert a_req in by_id, "tenant A's expired request missing from admin list"
    assert b_req in by_id, "tenant B's expired request missing from admin list"

    assert by_id[a_req].tenant_id == a_tenant
    assert by_id[b_req].tenant_id == b_tenant
    assert by_id[a_req].tenant_id != by_id[b_req].tenant_id, (
        "both rows came back with the same tenant_id — the admin list "
        "lost the per-row tenant attribution that downstream dispatch needs"
    )
