"""REST API tests for frontdesk v1.2 fields on the admin /agents
endpoints, the capacity advisory helper, and the approval list's
parent-walk filter.

Exercised contracts:

  * ``POST /api/admin/agents`` with ``is_frontdesk=True`` requires
    ``agent_role='super_agent'``; otherwise 400. The cross-check is
    application-level — there is no DB ``CHECK`` constraint — and a
    regression would let domain agents be flagged as the tenant's
    user-facing entry point.
  * ``PATCH /api/admin/agents/{id}`` validates the EFFECTIVE
    post-update values (a PATCH that only flips ``is_frontdesk=True``
    on an existing super_agent is accepted; flipping on a domain
    agent without simultaneously promoting it is rejected).
  * ``routing_description`` is editable on both create and update.
  * The capacity-advisory helper returns a warning when the tenant's
    container budget is below the required headroom, and None when
    it's adequate. Advisory only — never blocks the save.
  * ``GET /api/admin/approvals?conversation_id=...`` walks the parent
    so a parent-conv view surfaces approvals attributed to a child
    delegation conv.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)
from rolemesh.db.approval import create_approval_request
from webui import admin
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    app.include_router(admin.router)

    async def _return_user() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _return_user
    app.dependency_overrides[require_manage_agents] = _return_user
    app.dependency_overrides[require_manage_tenant] = _return_user
    app.dependency_overrides[require_manage_users] = _return_user
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )


def _authed_user(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role="owner",
        email="x@x.com", name="X",
    )


async def _seed_tenant_user() -> tuple[str, str]:
    t = await create_tenant(name="T", slug=f"fd-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="Alice",
        email=f"alice-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    return t.id, u.id


# ---------------------------------------------------------------------------
# is_frontdesk + agent_role validation on create
# ---------------------------------------------------------------------------


class TestCreateAgentFrontdeskValidation:
    @pytest.mark.asyncio
    async def test_is_frontdesk_true_requires_super_agent(self) -> None:
        """A domain agent (``agent_role='agent'``) flagged as the
        tenant's user-facing entry point is a confused state — the
        delegation handler in §6 Step 5 filters frontdesks out of the
        delegate-target catalog, so a frontdesk that's secretly a
        domain agent would be unreachable both as a target and as the
        catalog source. Reject at the admin layer with 400.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "frontdesk",
                    "folder": f"fd-{uuid.uuid4().hex[:8]}",
                    "agent_role": "agent",   # NOT super_agent
                    "is_frontdesk": True,
                },
            )
        assert resp.status_code == 400, resp.text
        # The error message must call out the offending invariant so
        # the operator knows which field to flip — not a generic
        # "validation failed".
        assert "super_agent" in resp.text

    @pytest.mark.asyncio
    async def test_super_agent_with_is_frontdesk_true_succeeds(self) -> None:
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "frontdesk",
                    "folder": f"fd-{uuid.uuid4().hex[:8]}",
                    "agent_role": "super_agent",
                    "is_frontdesk": True,
                },
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["is_frontdesk"] is True
        assert body["agent_role"] == "super_agent"

    @pytest.mark.asyncio
    async def test_default_is_frontdesk_false_is_unrestricted(self) -> None:
        """The validator must NOT fire on a normal create. A regression
        that always-checked would block every coworker creation.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "regular",
                    "folder": f"reg-{uuid.uuid4().hex[:8]}",
                    "agent_role": "agent",
                },
            )
        assert resp.status_code == 201
        assert resp.json()["is_frontdesk"] is False


# ---------------------------------------------------------------------------
# routing_description
# ---------------------------------------------------------------------------


class TestRoutingDescription:
    @pytest.mark.asyncio
    async def test_create_and_update_round_trip(self) -> None:
        """The routing_description is the capability card the
        frontdesk LLM reads when routing. It must survive a create
        round trip AND a PATCH update — both are operator surfaces.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            create_resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "trading",
                    "folder": f"trading-{uuid.uuid4().hex[:8]}",
                    "agent_role": "agent",
                    "routing_description": "Buy and sell stocks.",
                },
            )
            assert create_resp.status_code == 201
            aid = create_resp.json()["id"]
            assert create_resp.json()["routing_description"] == "Buy and sell stocks."

            patch_resp = await c.patch(
                f"/api/admin/agents/{aid}",
                json={"routing_description": "Buy, sell, and short stocks."},
            )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["routing_description"] == (
            "Buy, sell, and short stocks."
        )

    @pytest.mark.asyncio
    async def test_long_routing_description_rejected(self) -> None:
        """The length cap exists because the rendered catalog blob
        gets injected verbatim into every frontdesk's system prompt —
        a 5kB capability essay per specialist would balloon spawn-time
        context. 500 chars is generous but bounded.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "trading",
                    "folder": f"trading-{uuid.uuid4().hex[:8]}",
                    "agent_role": "agent",
                    "routing_description": "x" * 501,
                },
            )
        # Pydantic enforces max_length=500.
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH semantics — effective post-update values
# ---------------------------------------------------------------------------


class TestPatchFrontdeskFlip:
    @pytest.mark.asyncio
    async def test_patch_flipping_is_frontdesk_on_super_agent_succeeds(self) -> None:
        """Operator flow: create as super_agent, later flip is_frontdesk
        true to designate it as the tenant's user entry point. The
        EFFECTIVE post-update role is super_agent (unchanged), so this
        must be accepted even though the body itself does not re-send
        agent_role.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            create_resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "fd-candidate",
                    "folder": f"fdc-{uuid.uuid4().hex[:8]}",
                    "agent_role": "super_agent",
                },
            )
            assert create_resp.status_code == 201
            aid = create_resp.json()["id"]
            patch_resp = await c.patch(
                f"/api/admin/agents/{aid}",
                json={"is_frontdesk": True},
            )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_frontdesk"] is True

    @pytest.mark.asyncio
    async def test_patch_flipping_is_frontdesk_on_domain_agent_rejected(self) -> None:
        """A PATCH that flips is_frontdesk=True on an
        ``agent_role='agent'`` coworker must 400 — the effective role
        is still 'agent', violating the invariant.

        Mutation guard: a regression that validates against the BODY's
        agent_role (None here) instead of the EFFECTIVE role would
        silently accept this PATCH.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            create_resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "domain",
                    "folder": f"dom-{uuid.uuid4().hex[:8]}",
                    "agent_role": "agent",
                },
            )
            aid = create_resp.json()["id"]
            patch_resp = await c.patch(
                f"/api/admin/agents/{aid}",
                json={"is_frontdesk": True},
            )
        assert patch_resp.status_code == 400, patch_resp.text
        assert "super_agent" in patch_resp.text

    @pytest.mark.asyncio
    async def test_patch_promoting_role_and_flipping_in_same_call(self) -> None:
        """Operator can promote agent_role → super_agent AND set
        is_frontdesk=True atomically. The effective values both
        become super_agent + True, so this is allowed.
        """
        tid, uid = await _seed_tenant_user()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            create_resp = await c.post(
                "/api/admin/agents",
                json={
                    "name": "promoting",
                    "folder": f"prm-{uuid.uuid4().hex[:8]}",
                    "agent_role": "agent",
                },
            )
            aid = create_resp.json()["id"]
            patch_resp = await c.patch(
                f"/api/admin/agents/{aid}",
                json={"agent_role": "super_agent", "is_frontdesk": True},
            )
        assert patch_resp.status_code == 200, patch_resp.text
        body = patch_resp.json()
        assert body["is_frontdesk"] is True
        assert body["agent_role"] == "super_agent"


# ---------------------------------------------------------------------------
# Capacity advisory
# ---------------------------------------------------------------------------


class TestCapacityAdvisory:
    def test_warns_when_under_provisioned(self) -> None:
        """Concrete formula: peak_users * (1+3) + 2. With 3 peak users
        you need 14; if the tenant cap is 5 we must surface a warning
        with the exact numbers so the operator knows what to bump it
        to (not just "you're under-provisioned").
        """
        msg = admin.check_frontdesk_capacity(
            max_concurrent_containers=5,
            peak_concurrent_user_turns=3,
        )
        assert msg is not None
        assert "14" in msg  # required value
        assert "5" in msg   # current value

    def test_no_warning_when_adequate(self) -> None:
        msg = admin.check_frontdesk_capacity(
            max_concurrent_containers=20,
            peak_concurrent_user_turns=3,
        )
        assert msg is None

    def test_boundary_exactly_enough(self) -> None:
        """At exactly required = 1 + 3 + 2 = 6 for 1 peak user — no
        warning. Catches an off-by-one in the threshold comparison.
        """
        msg = admin.check_frontdesk_capacity(
            max_concurrent_containers=6,
            peak_concurrent_user_turns=1,
        )
        assert msg is None


# ---------------------------------------------------------------------------
# Approval list parent-walk
# ---------------------------------------------------------------------------


async def _mk_pending_approval(
    *, tenant_id: str, coworker_id: str, conversation_id: str, user_id: str,
) -> str:
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


class TestApprovalListParentWalk:
    @pytest.mark.asyncio
    async def test_endpoint_walks_parent_to_surface_child_approval(self) -> None:
        """A user (here an admin standing in for one) viewing the parent
        conversation hits ``/api/admin/approvals?conversation_id=<parent>``
        and gets back approvals that the specialist submitted while
        running in the delegation child. Without the parent walk this
        returns [] and the user has no path to the pending approval.
        """
        tid, uid = await _seed_tenant_user()
        # Build parent + child convs.
        fd = await create_coworker(
            tenant_id=tid, name="frontdesk",
            folder=f"fd-{uuid.uuid4().hex[:8]}",
        )
        fd_binding = await create_channel_binding(
            coworker_id=fd.id, tenant_id=tid,
            channel_type="web", credentials={},
        )
        parent_conv = await create_conversation(
            tenant_id=tid, coworker_id=fd.id,
            channel_binding_id=fd_binding.id,
            channel_chat_id=f"web-{uuid.uuid4().hex[:8]}",
            user_id=uid,
        )
        target = await create_coworker(
            tenant_id=tid, name="trading",
            folder=f"trading-{uuid.uuid4().hex[:8]}",
        )
        target_binding = await create_channel_binding(
            coworker_id=target.id, tenant_id=tid,
            channel_type="internal", credentials={},
        )
        child_conv = await create_conversation(
            tenant_id=tid, coworker_id=target.id,
            channel_binding_id=target_binding.id,
            channel_chat_id=f"internal:{parent_conv.id}:{target.id}",
            parent_conversation_id=parent_conv.id,
            requires_trigger=False,
        )
        child_req = await _mk_pending_approval(
            tenant_id=tid, coworker_id=target.id,
            conversation_id=child_conv.id, user_id=uid,
        )

        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as c:
            resp = await c.get(
                "/api/admin/approvals",
                params={"conversation_id": parent_conv.id},
            )
        assert resp.status_code == 200, resp.text
        ids = {row["id"] for row in resp.json()}
        assert child_req in ids
