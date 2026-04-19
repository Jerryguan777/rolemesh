"""REST API tests for /api/admin/approval-* endpoints.

Focus on the policy CRUD surface (happy path + cross-tenant isolation)
and the decide contract: 403 for non-approver, 409 for already-decided,
503 when the engine is not wired up. We patch the ``get_current_user``
dependency to bypass full OIDC plumbing — the endpoint's own
authorization is what we care about.

The engine is exercised by tests/approval/test_engine.py; here we want
to ensure the HTTP layer faithfully surfaces engine outcomes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.approval.engine import ApprovalEngine
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db.pg import (
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)
from webui import admin
from webui.dependencies import get_current_user

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class _FakePublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.publishes.append((subject, data))


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_to_conversation(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))


def _build_app(engine: ApprovalEngine | None, user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    app.include_router(admin.router)

    async def _authed() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _authed
    # Bypass permission checks; AdminUser/OwnerUser/UserManager all go through
    # get_current_user + user_can. For the approval endpoints that require
    # manage_agents, we grant the override's user an "admin" role.
    from webui.dependencies import (
        require_manage_agents,
        require_manage_tenant,
        require_manage_users,
    )

    async def _grant() -> AuthenticatedUser:
        return user

    app.dependency_overrides[require_manage_agents] = _grant
    app.dependency_overrides[require_manage_tenant] = _grant
    app.dependency_overrides[require_manage_users] = _grant

    admin.set_approval_engine(engine)
    return app


async def _seed_tenant() -> tuple[str, str, str, str]:
    """Returns (tenant_id, owner_user_id, coworker_id, conversation_id)."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(tenant_id=t.id, name="Owner", email="o@x.com", role="owner")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    c = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    return t.id, u.id, cw.id, c.id


def _authed_user(tenant_id: str, user_id: str, role: str = "owner") -> AuthenticatedUser:
    # AuthenticatedUser expects (user_id, tenant_id, role, email, name, …).
    return AuthenticatedUser(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email="x@x.com",
        name="X",
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    """AsyncClient pointed at the ASGI app — keeps everything on one loop."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _resolver() -> NotificationTargetResolver:
    async def _convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    async def _conv(conv_id: str) -> object | None:
        return object()

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_conv,
        webui_base_url=None,
    )


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------


class TestPolicyApi:
    async def test_create_list_get_update_delete(self) -> None:
        tenant_id, user_id, cw_id, _c = await _seed_tenant()
        app = _build_app(None, _authed_user(tenant_id, user_id))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/approval-policies",
                json={
                    "mcp_server_name": "erp",
                    "tool_name": "refund",
                    "condition_expr": {"field": "amount", "op": ">", "value": 100},
                    "coworker_id": cw_id,
                    "priority": 5,
                },
            )
            assert r.status_code == 201, r.text
            policy_id = r.json()["id"]

            r = await client.get("/api/admin/approval-policies")
            assert r.status_code == 200
            assert any(p["id"] == policy_id for p in r.json())

            r = await client.get(f"/api/admin/approval-policies/{policy_id}")
            assert r.status_code == 200
            assert r.json()["priority"] == 5

            r = await client.patch(
                f"/api/admin/approval-policies/{policy_id}",
                json={"priority": 42, "enabled": False},
            )
            assert r.status_code == 200
            assert r.json()["priority"] == 42
            assert r.json()["enabled"] is False

            r = await client.delete(f"/api/admin/approval-policies/{policy_id}")
            assert r.status_code == 204

            r = await client.get(f"/api/admin/approval-policies/{policy_id}")
            assert r.status_code == 404

    async def test_cross_tenant_policy_is_404(self) -> None:
        # Tenant A creates a policy; Tenant B's admin must not see it.
        ta, _ua, cwa, _ = await _seed_tenant()
        tb, ub, _, _ = await _seed_tenant()
        p = await create_approval_policy(
            tenant_id=ta,
            coworker_id=cwa,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"always": True},
        )
        app = _build_app(None, _authed_user(tb, ub))
        async with _client(app) as client:
            r = await client.get(f"/api/admin/approval-policies/{p.id}")
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# Approvals: list / detail / audit
# ---------------------------------------------------------------------------


async def _make_pending_request(
    tenant_id: str,
    user_id: str,
    cw_id: str,
    conv_id: str,
) -> str:
    p = await create_approval_policy(
        tenant_id=tenant_id,
        coworker_id=cw_id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[user_id],
    )
    req = await create_approval_request(
        tenant_id=tenant_id,
        coworker_id=cw_id,
        conversation_id=conv_id,
        policy_id=p.id,
        user_id=user_id,
        job_id="job-api",
        mcp_server_name="erp",
        actions=[{"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 500}}],
        action_hashes=["h"],
        rationale="r",
        source="proposal",
        status="pending",
        resolved_approvers=[user_id],
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    return req.id


class TestApprovalListAndGet:
    async def test_list_and_detail(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id = await _make_pending_request(tenant_id, user_id, cw_id, conv_id)
        app = _build_app(None, _authed_user(tenant_id, user_id))
        async with _client(app) as client:
            r = await client.get("/api/admin/approvals")
            assert r.status_code == 200
            assert any(item["id"] == req_id for item in r.json())

            r = await client.get(f"/api/admin/approvals/{req_id}")
            assert r.status_code == 200
            payload = r.json()
            assert payload["status"] == "pending"
            assert isinstance(payload["audit_log"], list)


# ---------------------------------------------------------------------------
# Decide contract
# ---------------------------------------------------------------------------


class TestDecideApi:
    async def test_decide_requires_engine(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id = await _make_pending_request(tenant_id, user_id, cw_id, conv_id)
        app = _build_app(None, _authed_user(tenant_id, user_id))
        async with _client(app) as client:
            r = await client.post(
                f"/api/admin/approvals/{req_id}/decide",
                json={"action": "approve"},
            )
            assert r.status_code == 503

    async def test_decide_returns_403_for_non_approver(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id = await _make_pending_request(tenant_id, user_id, cw_id, conv_id)
        outsider = await create_user(
            tenant_id=tenant_id, name="Eve", email="e@x.com", role="admin"
        )
        engine = ApprovalEngine(
            publisher=_FakePublisher(),
            channel_sender=_FakeChannel(),
            resolver=_resolver(),
        )
        app = _build_app(
            engine, _authed_user(tenant_id, outsider.id, role="admin")
        )
        async with _client(app) as client:
            r = await client.post(
                f"/api/admin/approvals/{req_id}/decide",
                json={"action": "approve"},
            )
            assert r.status_code == 403

    async def test_decide_returns_409_for_already_decided(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id = await _make_pending_request(tenant_id, user_id, cw_id, conv_id)
        engine = ApprovalEngine(
            publisher=_FakePublisher(),
            channel_sender=_FakeChannel(),
            resolver=_resolver(),
        )
        app = _build_app(engine, _authed_user(tenant_id, user_id))
        async with _client(app) as client:
            r = await client.post(
                f"/api/admin/approvals/{req_id}/decide",
                json={"action": "approve"},
            )
            assert r.status_code == 200
            r = await client.post(
                f"/api/admin/approvals/{req_id}/decide",
                json={"action": "reject", "note": "changed mind"},
            )
            assert r.status_code == 409

    async def test_decide_returns_404_for_cross_tenant_request(self) -> None:
        ta, ua, cwa, conv_a = await _seed_tenant()
        req_id = await _make_pending_request(ta, ua, cwa, conv_a)
        tb, ub, _, _ = await _seed_tenant()
        engine = ApprovalEngine(
            publisher=_FakePublisher(),
            channel_sender=_FakeChannel(),
            resolver=_resolver(),
        )
        app = _build_app(engine, _authed_user(tb, ub))
        async with _client(app) as client:
            r = await client.post(
                f"/api/admin/approvals/{req_id}/decide",
                json={"action": "approve"},
            )
            assert r.status_code == 404
