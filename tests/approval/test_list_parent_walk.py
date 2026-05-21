"""Parent-walk behaviour of ``list_approval_requests`` (frontdesk v1.2).

The handler attributes an approval to the child conversation a delegate
is running in. Without the parent walk, the user — who only sees the
parent conv in their conversation list — would not see those approvals
at all. The walk SHOULD:

- match rows where ``conversation_id`` equals the supplied id, AND
- match rows where ``conversation_id`` belongs to a conv whose
  ``parent_conversation_id`` equals the supplied id, AND
- NOT match unrelated conversations (cross-tenant or otherwise).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)
from rolemesh.db.approval import create_approval_request, list_approval_requests

pytestmark = pytest.mark.usefixtures("test_db")


async def _make_user_conv() -> tuple[str, str, str, str]:
    """Return ``(tenant_id, user_id, coworker_id, parent_conversation_id)``."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(tenant_id=t.id, name="Alice", role="owner")
    cw = await create_coworker(
        tenant_id=t.id, name="frontdesk", folder=f"fd-{uuid.uuid4().hex[:8]}",
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
        user_id=u.id,
    )
    return t.id, u.id, cw.id, conv.id


async def _make_child_conv(
    tenant_id: str, parent_conv_id: str,
) -> tuple[str, str]:
    """Create a target coworker + internal child conv hanging off parent.

    Returns ``(target_coworker_id, child_conv_id)``.
    """
    target = await create_coworker(
        tenant_id=tenant_id,
        name="trading",
        folder=f"trading-{uuid.uuid4().hex[:8]}",
    )
    binding = await create_channel_binding(
        coworker_id=target.id,
        tenant_id=tenant_id,
        channel_type="internal",
        credentials={},
    )
    child = await create_conversation(
        tenant_id=tenant_id,
        coworker_id=target.id,
        channel_binding_id=binding.id,
        channel_chat_id=f"internal:{parent_conv_id}:{target.id}",
        parent_conversation_id=parent_conv_id,
        requires_trigger=False,
    )
    return target.id, child.id


async def _mk_approval(
    *, tenant_id: str, coworker_id: str, conversation_id: str, user_id: str,
) -> str:
    """Insert a pending approval row. Returns request id."""
    r = await create_approval_request(
        tenant_id=tenant_id,
        coworker_id=coworker_id,
        conversation_id=conversation_id,
        policy_id=None,
        user_id=user_id,
        job_id=f"job-{uuid.uuid4().hex[:8]}",
        mcp_server_name="mcp",
        actions=[{"tool_name": "x"}],
        action_hashes=[uuid.uuid4().hex],
        rationale=None,
        source="proposal",
        status="pending",
        resolved_approvers=[user_id],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return r.id


class TestParentWalk:
    async def test_returns_only_direct_match_without_parent_walk_param(self) -> None:
        """The walk only fires when ``conversation_id`` filter is set —
        existing callers that pass coworker_id alone keep behaving
        the same way they always did. Catches a refactor that
        accidentally inlines the walk into the unfiltered query.
        """
        tenant_id, user_id, cw_id, parent_id = await _make_user_conv()
        target_id, child_id = await _make_child_conv(tenant_id, parent_id)
        parent_req = await _mk_approval(
            tenant_id=tenant_id, coworker_id=cw_id,
            conversation_id=parent_id, user_id=user_id,
        )
        child_req = await _mk_approval(
            tenant_id=tenant_id, coworker_id=target_id,
            conversation_id=child_id, user_id=user_id,
        )
        rows = await list_approval_requests(tenant_id=tenant_id)
        ids = {r.id for r in rows}
        # Without the conversation_id filter, both are returned — no walk.
        assert parent_req in ids
        assert child_req in ids

    async def test_parent_walk_returns_child_approval_to_parent_viewer(self) -> None:
        """A user viewing their parent conv must see the child's approval.

        Mutation guard: this assertion fails if the SQL is reduced to
        ``conversation_id = $cid`` (no UNION/IN), confirming the walk
        is what surfaces the child row.
        """
        tenant_id, user_id, _cw_id, parent_id = await _make_user_conv()
        target_id, child_id = await _make_child_conv(tenant_id, parent_id)
        child_req = await _mk_approval(
            tenant_id=tenant_id, coworker_id=target_id,
            conversation_id=child_id, user_id=user_id,
        )
        rows = await list_approval_requests(
            tenant_id=tenant_id, conversation_id=parent_id,
        )
        assert {r.id for r in rows} == {child_req}

    async def test_parent_walk_returns_direct_match_too(self) -> None:
        """The walk widens the match — it does NOT replace it. Both an
        approval attributed directly to the parent AND any child
        approval show up in one query.
        """
        tenant_id, user_id, cw_id, parent_id = await _make_user_conv()
        target_id, child_id = await _make_child_conv(tenant_id, parent_id)
        parent_req = await _mk_approval(
            tenant_id=tenant_id, coworker_id=cw_id,
            conversation_id=parent_id, user_id=user_id,
        )
        child_req = await _mk_approval(
            tenant_id=tenant_id, coworker_id=target_id,
            conversation_id=child_id, user_id=user_id,
        )
        rows = await list_approval_requests(
            tenant_id=tenant_id, conversation_id=parent_id,
        )
        assert {r.id for r in rows} == {parent_req, child_req}

    async def test_parent_walk_skips_unrelated_conversations(self) -> None:
        """A parent conv with no children must not pick up approvals
        from someone else's conversation — the walk's IN-clause is
        bounded by ``parent_conversation_id = $cid``.
        """
        tenant_id, user_id, cw_id, parent_id = await _make_user_conv()
        # A sibling conversation under the same tenant + coworker, but
        # NOT a child of the queried parent.
        other_b = await create_channel_binding(
            coworker_id=cw_id,
            tenant_id=tenant_id,
            channel_type="slack",
            credentials={"bot_token": "y"},
        )
        other_conv = await create_conversation(
            tenant_id=tenant_id,
            coworker_id=cw_id,
            channel_binding_id=other_b.id,
            channel_chat_id=str(uuid.uuid4()),
            user_id=user_id,
        )
        _stranger = await _mk_approval(
            tenant_id=tenant_id, coworker_id=cw_id,
            conversation_id=other_conv.id, user_id=user_id,
        )
        rows = await list_approval_requests(
            tenant_id=tenant_id, conversation_id=parent_id,
        )
        assert rows == []

    async def test_parent_walk_does_not_cross_tenant(self) -> None:
        """Tenant isolation must hold even with the parent-walk SQL —
        a tenant scoping the parent walk to a conv id should not see
        another tenant's approvals even on a UUID collision.
        """
        # Tenant A
        ta, _ua, _cwa_id, _parent_a = await _make_user_conv()
        # Tenant B — separate world
        tb, ub, cwb_id, parent_b = await _make_user_conv()
        # An approval in tenant B's conv.
        b_only = await _mk_approval(
            tenant_id=tb, coworker_id=cwb_id,
            conversation_id=parent_b, user_id=ub,
        )
        # Query tenant A scoped to tenant B's conversation_id — must
        # return nothing.
        rows = await list_approval_requests(
            tenant_id=ta, conversation_id=parent_b,
        )
        assert rows == []
        # Sanity: tenant B can still see its own.
        rows_b = await list_approval_requests(
            tenant_id=tb, conversation_id=parent_b,
        )
        assert {r.id for r in rows_b} == {b_only}
