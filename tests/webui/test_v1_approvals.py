"""Integration tests for ``/api/v1/approval-policies`` and
``/api/v1/approvals``.

Hits the FastAPI app via httpx ASGI transport against a real
Postgres testcontainer (per tests/conftest.py). The legacy
``/api/admin/approval*`` surface stays in place for the 6-month
compatibility window — these tests cover the new v1 surface end-
to-end and pin the INV-4 / INV-7 / RLS / SET NULL invariants
called out in the 03a session prompt.
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
from rolemesh.auth.bootstrap_actor import BOOTSTRAP_USER_LITERAL
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    _get_admin_pool,
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
)
from webui.api_v1 import router as api_v1_router
from webui.dependencies import get_current_user
from webui.main import _bootstrap_actor_error_handler
from webui.v1.approval_engine_registry import set_approval_engine
from webui.v1.errors import install_error_handler

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class _FakePublisher:
    """Captures NATS publishes so PR2's event-emit tests can reuse
    the same shape. Here we only need the engine to think a
    publisher is wired — we never assert anything against it
    because the engine's NATS contract belongs to PR2."""

    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.publishes.append((subject, data))


class _FakeChannel:
    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        return


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


def _engine() -> ApprovalEngine:
    return ApprovalEngine(
        publisher=_FakePublisher(),
        channel_sender=_FakeChannel(),
        resolver=_resolver(),
    )


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    install_error_handler(app)

    # INV-4 hand-off: the v1 decide endpoint surfaces
    # BootstrapActorError as a deterministic 503 envelope.
    from rolemesh.auth.bootstrap_actor import BootstrapActorError

    app.add_exception_handler(
        BootstrapActorError, _bootstrap_actor_error_handler,
    )
    app.include_router(api_v1_router)

    async def _authed() -> AuthenticatedUser:
        return user

    app.dependency_overrides[get_current_user] = _authed
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _authed_user(
    tenant_id: str, user_id: str, role: str = "owner"
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        email="x@x.com",
        name="X",
    )


async def _seed_tenant(slug_prefix: str = "v1") -> tuple[str, str, str, str]:
    """Returns (tenant_id, owner_user_id, coworker_id, conversation_id)."""
    t = await create_tenant(
        name=f"T-{slug_prefix}", slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    u = await create_user(
        tenant_id=t.id, name="Owner",
        email=f"o-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    b = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    c = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    return t.id, u.id, cw.id, c.id


async def _make_pending(
    tenant_id: str,
    requester_id: str,
    cw_id: str,
    conv_id: str,
    *,
    approvers: list[str] | None = None,
) -> tuple[str, str]:
    """Insert a pending request; returns (request_id, policy_id)."""
    p = await create_approval_policy(
        tenant_id=tenant_id, coworker_id=cw_id,
        mcp_server_name="erp", tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=approvers or [requester_id],
    )
    req = await create_approval_request(
        tenant_id=tenant_id, coworker_id=cw_id,
        conversation_id=conv_id, policy_id=p.id,
        user_id=requester_id, job_id=f"job-{uuid.uuid4().hex[:6]}",
        mcp_server_name="erp",
        actions=[
            {"mcp_server": "erp", "tool_name": "refund",
             "params": {"amount": 500}}
        ],
        action_hashes=[f"h-{uuid.uuid4().hex[:6]}"],
        rationale="r", source="proposal", status="pending",
        resolved_approvers=approvers or [requester_id],
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    return req.id, p.id


# ---------------------------------------------------------------------------
# Policy CRUD
# ---------------------------------------------------------------------------


class TestPolicyCrud:
    async def test_create_list_get_patch_delete(self) -> None:
        tenant_id, user_id, cw_id, _ = await _seed_tenant()
        app = _build_app(_authed_user(tenant_id, user_id))
        async with _client(app) as ac:
            r = await ac.post(
                "/api/v1/approval-policies",
                json={
                    "mcp_server_name": "erp",
                    "tool_name": "refund",
                    "condition_expr": {"always": True},
                    "coworker_id": cw_id,
                    "priority": 7,
                },
            )
            assert r.status_code == 201, r.text
            policy_id = r.json()["id"]

            r = await ac.get("/api/v1/approval-policies")
            assert r.status_code == 200
            assert any(p["id"] == policy_id for p in r.json())

            r = await ac.get(f"/api/v1/approval-policies/{policy_id}")
            assert r.status_code == 200
            assert r.json()["priority"] == 7

            r = await ac.patch(
                f"/api/v1/approval-policies/{policy_id}",
                json={"priority": 42, "enabled": False},
            )
            assert r.status_code == 200
            assert r.json()["priority"] == 42
            assert r.json()["enabled"] is False

            r = await ac.delete(f"/api/v1/approval-policies/{policy_id}")
            assert r.status_code == 204

            r = await ac.get(f"/api/v1/approval-policies/{policy_id}")
            assert r.status_code == 404
            assert r.json()["code"] == "NOT_FOUND"

    async def test_member_role_cannot_create_policy(self) -> None:
        """Policy mutation needs admin+; members get 403 with envelope."""
        tenant_id, owner_id, cw_id, _ = await _seed_tenant()
        member = await create_user(
            tenant_id=tenant_id, name="Eve",
            email=f"e-{uuid.uuid4().hex[:6]}@x.com", role="member",
        )
        app = _build_app(_authed_user(tenant_id, member.id, role="member"))
        async with _client(app) as ac:
            r = await ac.post(
                "/api/v1/approval-policies",
                json={
                    "mcp_server_name": "erp",
                    "tool_name": "refund",
                    "condition_expr": {"always": True},
                    "coworker_id": cw_id,
                },
            )
            assert r.status_code == 403
            assert r.json()["code"] == "FORBIDDEN"

    async def test_cross_tenant_policy_returns_404(self) -> None:
        """RLS isolation: tenant B can't see tenant A's policy."""
        ta, _, cwa, _ = await _seed_tenant("a")
        p = await create_approval_policy(
            tenant_id=ta, coworker_id=cwa,
            mcp_server_name="erp", tool_name="refund",
            condition_expr={"always": True},
        )
        tb, ub, _, _ = await _seed_tenant("b")
        app = _build_app(_authed_user(tb, ub))
        async with _client(app) as ac:
            r = await ac.get(f"/api/v1/approval-policies/{p.id}")
            assert r.status_code == 404
            r = await ac.patch(
                f"/api/v1/approval-policies/{p.id}",
                json={"enabled": False},
            )
            assert r.status_code == 404

    async def test_delete_policy_sets_pending_request_policy_to_null(
        self,
    ) -> None:
        """DELETE policy must not cascade-delete already-issued requests.

        ``approval_requests.policy_id`` is ``ON DELETE SET NULL`` —
        the design §3 DELETE 语义 table mandates that an in-flight
        approval survives a policy retraction.
        """
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id, policy_id = await _make_pending(
            tenant_id, user_id, cw_id, conv_id,
        )
        app = _build_app(_authed_user(tenant_id, user_id))
        async with _client(app) as ac:
            r = await ac.delete(f"/api/v1/approval-policies/{policy_id}")
            assert r.status_code == 204
            r = await ac.get(f"/api/v1/approvals/{req_id}")
            assert r.status_code == 200
            payload = r.json()
            assert payload["policy_id"] is None
            assert payload["status"] == "pending"

    async def test_create_with_coworker_from_other_tenant_returns_422(
        self,
    ) -> None:
        """Cross-tenant coworker_id must be rejected with a typed code."""
        ta, ua, cwa, _ = await _seed_tenant("a")
        tb, ub, _, _ = await _seed_tenant("b")
        app = _build_app(_authed_user(tb, ub))
        async with _client(app) as ac:
            r = await ac.post(
                "/api/v1/approval-policies",
                json={
                    "mcp_server_name": "erp",
                    "tool_name": "refund",
                    "condition_expr": {"always": True},
                    "coworker_id": cwa,
                },
            )
            assert r.status_code == 422
            assert r.json()["code"] == "INVALID_COWORKER"


# ---------------------------------------------------------------------------
# Listing + filtering
# ---------------------------------------------------------------------------


class TestListing:
    async def test_default_scope_filters_to_me_as_approver(self) -> None:
        """Default ``scope=mine`` hides requests where the caller is
        not in ``resolved_approvers``.
        """
        tenant_id, alice_id, cw_id, conv_id = await _seed_tenant()
        bob = await create_user(
            tenant_id=tenant_id, name="Bob",
            email=f"b-{uuid.uuid4().hex[:6]}@x.com", role="owner",
        )
        # Two requests: one where bob is the approver, one where
        # alice is. As bob, default listing should only show the
        # first.
        bob_req, _ = await _make_pending(
            tenant_id, alice_id, cw_id, conv_id, approvers=[bob.id]
        )
        alice_req, _ = await _make_pending(
            tenant_id, alice_id, cw_id, conv_id, approvers=[alice_id]
        )
        app = _build_app(_authed_user(tenant_id, bob.id))
        async with _client(app) as ac:
            r = await ac.get("/api/v1/approvals")
            assert r.status_code == 200
            ids = [row["id"] for row in r.json()]
            assert bob_req in ids
            assert alice_req not in ids

    async def test_scope_all_returns_full_tenant_for_admins(self) -> None:
        tenant_id, alice_id, cw_id, conv_id = await _seed_tenant()
        bob = await create_user(
            tenant_id=tenant_id, name="Bob",
            email=f"b-{uuid.uuid4().hex[:6]}@x.com", role="owner",
        )
        bob_req, _ = await _make_pending(
            tenant_id, alice_id, cw_id, conv_id, approvers=[bob.id]
        )
        alice_req, _ = await _make_pending(
            tenant_id, alice_id, cw_id, conv_id, approvers=[alice_id]
        )
        app = _build_app(_authed_user(tenant_id, bob.id, role="owner"))
        async with _client(app) as ac:
            r = await ac.get("/api/v1/approvals?scope=all")
            assert r.status_code == 200
            ids = [row["id"] for row in r.json()]
            assert bob_req in ids
            assert alice_req in ids

    async def test_scope_all_is_403_for_member(self) -> None:
        tenant_id, _, _, _ = await _seed_tenant()
        member = await create_user(
            tenant_id=tenant_id, name="member",
            email=f"m-{uuid.uuid4().hex[:6]}@x.com", role="member",
        )
        app = _build_app(_authed_user(tenant_id, member.id, role="member"))
        async with _client(app) as ac:
            r = await ac.get("/api/v1/approvals?scope=all")
            assert r.status_code == 403
            assert r.json()["code"] == "FORBIDDEN"

    async def test_cross_tenant_listing_is_isolated(self) -> None:
        ta, ua, cwa, conv_a = await _seed_tenant("a")
        tb, ub, _, _ = await _seed_tenant("b")
        # Seed a request in tenant A.
        await _make_pending(ta, ua, cwa, conv_a)
        app = _build_app(_authed_user(tb, ub, role="owner"))
        async with _client(app) as ac:
            r = await ac.get("/api/v1/approvals?scope=all")
            assert r.status_code == 200
            assert r.json() == []


# ---------------------------------------------------------------------------
# Detail + audit log
# ---------------------------------------------------------------------------


class TestDetailAndAudit:
    async def test_detail_includes_inline_audit_log(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id, _ = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        app = _build_app(_authed_user(tenant_id, user_id))
        async with _client(app) as ac:
            r = await ac.get(f"/api/v1/approvals/{req_id}")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "pending"
            # 'created' is written by the DB trigger on insert.
            actions = [e["action"] for e in body["audit_log"]]
            assert "created" in actions

    async def test_audit_log_endpoint_returns_same_rows(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id, _ = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        app = _build_app(_authed_user(tenant_id, user_id))
        async with _client(app) as ac:
            r = await ac.get(f"/api/v1/approvals/{req_id}/audit-log")
            assert r.status_code == 200
            actions = [e["action"] for e in r.json()]
            assert "created" in actions

    async def test_cross_tenant_detail_returns_404(self) -> None:
        ta, ua, cwa, conv_a = await _seed_tenant("a")
        req_id, _ = await _make_pending(ta, ua, cwa, conv_a)
        tb, ub, _, _ = await _seed_tenant("b")
        app = _build_app(_authed_user(tb, ub))
        async with _client(app) as ac:
            r = await ac.get(f"/api/v1/approvals/{req_id}")
            assert r.status_code == 404
            r = await ac.get(f"/api/v1/approvals/{req_id}/audit-log")
            assert r.status_code == 404


# ---------------------------------------------------------------------------
# Decide — INV-4 + INV-7 end-to-end
# ---------------------------------------------------------------------------


async def _fetch_audit_rows(
    tenant_id: str, request_id: str
) -> list[dict[str, Any]]:
    """Direct DB read; bypasses the API so the test asserts what
    the trigger actually wrote, not what the API projects.
    """
    pool = _get_admin_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, actor_user_id::text AS actor_user_id, note "
            "FROM approval_audit_log "
            "WHERE tenant_id = $1::uuid AND request_id = $2::uuid "
            "ORDER BY created_at ASC",
            tenant_id, request_id,
        )
    return [dict(r) for r in rows]


class TestDecide:
    async def test_returns_503_when_engine_unavailable(self) -> None:
        set_approval_engine(None)
        tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
        req_id, _ = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        app = _build_app(_authed_user(tenant_id, user_id))
        async with _client(app) as ac:
            r = await ac.post(
                f"/api/v1/approvals/{req_id}/decide",
                json={"action": "approve"},
            )
            assert r.status_code == 503
            assert r.json()["code"] == "APPROVAL_ENGINE_UNAVAILABLE"

    async def test_approve_writes_audit_row_with_real_actor_uuid(
        self,
    ) -> None:
        """INV-4: ``actor_user_id`` must be a real UUID (not the
        literal ``"bootstrap"``) on the decide audit row.
        """
        set_approval_engine(_engine())
        try:
            tenant_id, alice, cw_id, conv_id = await _seed_tenant()
            bob = await create_user(
                tenant_id=tenant_id, name="Bob",
                email=f"b-{uuid.uuid4().hex[:6]}@x.com", role="owner",
            )
            req_id, _ = await _make_pending(
                tenant_id, alice, cw_id, conv_id, approvers=[bob.id]
            )
            app = _build_app(_authed_user(tenant_id, bob.id, role="owner"))
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "approve", "note": "looks fine"},
                )
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "approved"
            rows = await _fetch_audit_rows(tenant_id, req_id)
            decide_rows = [r for r in rows if r["action"] == "approved"]
            assert decide_rows, f"no 'approved' audit row in {rows!r}"
            assert decide_rows[0]["actor_user_id"] == bob.id
            uuid.UUID(decide_rows[0]["actor_user_id"])
        finally:
            set_approval_engine(None)

    async def test_reject_writes_audit_row_with_real_actor_uuid(self) -> None:
        set_approval_engine(_engine())
        try:
            tenant_id, alice, cw_id, conv_id = await _seed_tenant()
            bob = await create_user(
                tenant_id=tenant_id, name="Bob",
                email=f"b-{uuid.uuid4().hex[:6]}@x.com", role="owner",
            )
            req_id, _ = await _make_pending(
                tenant_id, alice, cw_id, conv_id, approvers=[bob.id]
            )
            app = _build_app(_authed_user(tenant_id, bob.id, role="owner"))
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "reject", "note": "no"},
                )
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "rejected"
            rows = await _fetch_audit_rows(tenant_id, req_id)
            decide_rows = [r for r in rows if r["action"] == "rejected"]
            assert decide_rows
            assert decide_rows[0]["actor_user_id"] == bob.id
            uuid.UUID(decide_rows[0]["actor_user_id"])
        finally:
            set_approval_engine(None)

    async def test_bootstrap_token_decide_falls_back_to_tenant_owner(
        self,
    ) -> None:
        """INV-4 fallback: when AuthenticatedUser.user_id is the
        ``"bootstrap"`` literal, ``resolve_actor_user_id`` writes
        the tenant owner's UUID. The pinned test in
        tests/test_audit_actor_resolution.py covers the helper in
        isolation; this anchors the end-to-end audit row.
        """
        set_approval_engine(_engine())
        try:
            tenant_id, owner_id, cw_id, conv_id = await _seed_tenant()
            # Approver list contains the owner UUID — the helper
            # rewrites the bootstrap actor to the owner, so the
            # decide CTE finds them in resolved_approvers.
            req_id, _ = await _make_pending(
                tenant_id, owner_id, cw_id, conv_id,
                approvers=[owner_id],
            )
            # Simulate the bootstrap fast-path: AuthenticatedUser
            # carries the literal user_id string.
            bootstrap_user = AuthenticatedUser(
                user_id=BOOTSTRAP_USER_LITERAL,
                tenant_id=tenant_id, role="owner",
                email="x@x.com", name="bootstrap",
            )
            app = _build_app(bootstrap_user)
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "approve"},
                )
                assert r.status_code == 200, r.text
            rows = await _fetch_audit_rows(tenant_id, req_id)
            decide_rows = [r for r in rows if r["action"] == "approved"]
            assert decide_rows
            assert decide_rows[0]["actor_user_id"] == owner_id
        finally:
            set_approval_engine(None)

    async def test_bootstrap_decide_without_owner_returns_503(self) -> None:
        """INV-4 hard error path: tenant has no owner →
        BootstrapActorError → 503 with ``BOOTSTRAP_NEEDS_TENANT_OWNER``.
        """
        set_approval_engine(_engine())
        try:
            # Tenant + coworker + conversation, but NO owner — we
            # only create a member user so the resolver fails.
            tenant_id, owner_id, cw_id, conv_id = await _seed_tenant()
            # Delete the owner the seeder created so the resolver
            # has nothing to fall back to. Simplest: just use a
            # fresh tenant with only a non-owner approver.
            t2 = await create_tenant(
                name="no-owner",
                slug=f"no-owner-{uuid.uuid4().hex[:8]}",
            )
            member = await create_user(
                tenant_id=t2.id, name="member",
                email=f"m-{uuid.uuid4().hex[:6]}@x.com", role="member",
            )
            cw2 = await create_coworker(
                tenant_id=t2.id, name="cw2",
                folder=f"cw-{uuid.uuid4().hex[:8]}",
            )
            b2 = await create_channel_binding(
                coworker_id=cw2.id, tenant_id=t2.id,
                channel_type="telegram",
                credentials={"bot_token": "x"},
            )
            c2 = await create_conversation(
                tenant_id=t2.id, coworker_id=cw2.id,
                channel_binding_id=b2.id,
                channel_chat_id=str(uuid.uuid4()),
            )
            req_id, _ = await _make_pending(
                t2.id, member.id, cw2.id, c2.id,
                approvers=[member.id],
            )
            bootstrap_user = AuthenticatedUser(
                user_id=BOOTSTRAP_USER_LITERAL,
                tenant_id=t2.id, role="owner",
                email="x@x.com", name="bootstrap",
            )
            app = _build_app(bootstrap_user)
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "approve"},
                )
                assert r.status_code == 503
                assert r.json()["code"] == "BOOTSTRAP_NEEDS_TENANT_OWNER"
        finally:
            set_approval_engine(None)

    async def test_decide_returns_403_for_non_approver(self) -> None:
        set_approval_engine(_engine())
        try:
            tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
            req_id, _ = await _make_pending(
                tenant_id, user_id, cw_id, conv_id,
            )
            outsider = await create_user(
                tenant_id=tenant_id, name="Eve",
                email=f"e-{uuid.uuid4().hex[:6]}@x.com", role="admin",
            )
            app = _build_app(
                _authed_user(tenant_id, outsider.id, role="admin"),
            )
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "approve"},
                )
                assert r.status_code == 403
                assert r.json()["code"] == "FORBIDDEN"
        finally:
            set_approval_engine(None)

    async def test_decide_returns_409_for_already_decided(self) -> None:
        set_approval_engine(_engine())
        try:
            tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
            req_id, _ = await _make_pending(
                tenant_id, user_id, cw_id, conv_id,
            )
            app = _build_app(_authed_user(tenant_id, user_id))
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "approve"},
                )
                assert r.status_code == 200
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "reject"},
                )
                assert r.status_code == 409
                assert r.json()["code"] == "ALREADY_DECIDED"
                assert r.json()["details"]["current_status"] == "approved"
        finally:
            set_approval_engine(None)

    async def test_decide_returns_404_for_cross_tenant_request(self) -> None:
        set_approval_engine(_engine())
        try:
            ta, ua, cwa, conv_a = await _seed_tenant("a")
            req_id, _ = await _make_pending(ta, ua, cwa, conv_a)
            tb, ub, _, _ = await _seed_tenant("b")
            app = _build_app(_authed_user(tb, ub))
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "approve"},
                )
                assert r.status_code == 404
                assert r.json()["code"] == "NOT_FOUND"
        finally:
            set_approval_engine(None)

    async def test_decide_with_invalid_action_returns_422(self) -> None:
        """Pydantic gates ``action`` to ``approve|reject``; an
        out-of-vocabulary value must produce a typed 422, not 500.
        """
        set_approval_engine(_engine())
        try:
            tenant_id, user_id, cw_id, conv_id = await _seed_tenant()
            req_id, _ = await _make_pending(
                tenant_id, user_id, cw_id, conv_id,
            )
            app = _build_app(_authed_user(tenant_id, user_id))
            async with _client(app) as ac:
                r = await ac.post(
                    f"/api/v1/approvals/{req_id}/decide",
                    json={"action": "shrug"},
                )
                # FastAPI's request validation rejects this before
                # the handler runs — yields the default 422 envelope.
                assert r.status_code == 422
        finally:
            set_approval_engine(None)
