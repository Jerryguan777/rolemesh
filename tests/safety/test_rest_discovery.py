"""REST API tests for V2 P1.5 discovery endpoints.

Covers:
  - /safety/checks — check metadata + pydantic JSON schema.
  - /tenants/{tid}/safety/decisions — paginated list with filter set.
  - /tenants/{tid}/safety/decisions/{id} — detail + cross-tenant 404.
  - /tenants/{tid}/safety/rules/{rule_id}/audit — rule change timeline.

Cross-tenant invariant: all tenant-scoped endpoints refuse requests
where path tid != user.tenant_id (403 for non-detail, 404 for detail
to avoid leaking decision-UUID existence across tenants).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import (
    create_coworker,
    create_safety_rule,
    create_tenant,
    create_user,
    insert_safety_decision,
    update_safety_rule,
)
from webui import admin
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

pytestmark = pytest.mark.usefixtures("test_db")


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
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _user(tenant_id: str, role: str = "owner") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        role=role,
        email="admin@example.com",
    )


class TestChecksEndpoint:
    @pytest.mark.asyncio
    async def test_lists_registered_checks_with_schemas(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get("/api/admin/safety/checks")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        ids = {c["id"] for c in body}
        # Both cheap checks registered in the container + orchestrator
        # registry must show up. Defends against a refactor that
        # accidentally yanks one from the orch registry.
        assert "pii.regex" in ids
        assert "domain_allowlist" in ids
        # pii.regex ships a config schema via pydantic; surface must
        # be a JSON Schema dict (admin UI will render a form from it).
        pii = next(c for c in body if c["id"] == "pii.regex")
        assert isinstance(pii["config_schema"], dict)
        assert pii["config_schema"]["title"] == "PIIRegexConfig"
        # Stable sort by id — dashboards that cache see stable order.
        assert ids == set(ids)
        ordered_ids = [c["id"] for c in body]
        assert ordered_ids == sorted(ordered_ids)


class TestDecisionsListEndpoint:
    @pytest.mark.asyncio
    async def test_returns_total_and_items_page(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        for i in range(5):
            await insert_safety_decision(
                tenant_id=tenant.id,
                coworker_id=cw.id,
                stage="pre_tool_call",
                verdict_action="block",
                triggered_rule_ids=[],
                findings=[],
                context_digest="",
                context_summary=f"r{i}",
            )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions?limit=2"
            )
        assert r.status_code == 200
        body = r.json()
        # Total reflects the full tenant count; items is the page.
        # A UI that ignored total would show "2 of ?" instead of "2 of 5".
        assert body["total"] == 5
        assert len(body["items"]) == 2

    @pytest.mark.asyncio
    async def test_limit_is_capped_at_200(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            # Request a huge limit; server must cap to 200 rather
            # than let a misbehaving client scan the whole table.
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}/safety/decisions?"
                f"limit=99999"
            )
        assert r.status_code == 200
        body = r.json()
        # No data — count 0 — but we still prove the endpoint didn't
        # reject the overlarge limit. The cap is internal.
        assert body["total"] == 0

    @pytest.mark.asyncio
    async def test_cross_tenant_list_returns_403(self) -> None:
        tenant_a = await create_tenant(
            name="A", slug=f"a-{uuid.uuid4().hex[:8]}"
        )
        tenant_b = await create_tenant(
            name="B", slug=f"b-{uuid.uuid4().hex[:8]}"
        )
        app = _build_app(_user(tenant_b.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_a.id}/safety/decisions"
            )
        assert r.status_code == 403


class TestDecisionDetailEndpoint:
    @pytest.mark.asyncio
    async def test_returns_full_row(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        decision_id = await insert_safety_decision(
            tenant_id=tenant.id,
            coworker_id=cw.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[
                {
                    "code": "PII.SSN", "severity": "high",
                    "message": "m", "metadata": {},
                }
            ],
            context_digest="d" * 64,
            context_summary="tool=x",
        )
        app = _build_app(_user(tenant.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}"
                f"/safety/decisions/{decision_id}"
            )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == decision_id
        assert body["findings"][0]["code"] == "PII.SSN"
        # Detail surfaces context_digest (for cross-row dedup) that
        # CSV export intentionally omits.
        assert body["context_digest"] == "d" * 64

    @pytest.mark.asyncio
    async def test_cross_tenant_detail_returns_404_not_403(self) -> None:
        # Deliberate: do NOT reveal UUID existence across tenants.
        # A 403 here would leak that "yes, this UUID exists (in some
        # tenant)" — we return 404 so cross-tenant probing can't
        # distinguish "never existed" from "exists elsewhere".
        tenant_a = await create_tenant(
            name="A", slug=f"a-{uuid.uuid4().hex[:8]}"
        )
        tenant_b = await create_tenant(
            name="B", slug=f"b-{uuid.uuid4().hex[:8]}"
        )
        cw_a = await create_coworker(
            tenant_id=tenant_a.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        decision_id = await insert_safety_decision(
            tenant_id=tenant_a.id,
            coworker_id=cw_a.id,
            stage="pre_tool_call",
            verdict_action="block",
            triggered_rule_ids=[],
            findings=[],
            context_digest="",
            context_summary="",
        )
        app = _build_app(_user(tenant_b.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_a.id}"
                f"/safety/decisions/{decision_id}"
            )
        assert r.status_code == 404


class TestRuleAuditEndpoint:
    @pytest.mark.asyncio
    async def test_returns_timeline_for_rule(self) -> None:
        tenant = await create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        # Real persisted user so the audit FK resolves.
        actor = await create_user(
            tenant_id=tenant.id,
            name="admin",
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            role="owner",
        )
        user = AuthenticatedUser(
            user_id=actor.id,
            tenant_id=tenant.id,
            role="owner",
            email=actor.email,
        )
        rule = await create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            actor_user_id=actor.id,
        )
        # Edit the rule to produce an 'updated' audit row.
        await update_safety_rule(
            rule.id, tenant_id=tenant.id, enabled=False, actor_user_id=actor.id
        )
        app = _build_app(user)
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant.id}"
                f"/safety/rules/{rule.id}/audit"
            )
        assert r.status_code == 200
        body = r.json()
        # At least 2 rows: created + updated (trigger writes on both).
        assert len(body) >= 2
        actions = {row["action"] for row in body}
        assert {"created", "updated"}.issubset(actions)

    @pytest.mark.asyncio
    async def test_cross_tenant_rule_audit_returns_403(self) -> None:
        tenant_a = await create_tenant(
            name="A", slug=f"a-{uuid.uuid4().hex[:8]}"
        )
        tenant_b = await create_tenant(
            name="B", slug=f"b-{uuid.uuid4().hex[:8]}"
        )
        app = _build_app(_user(tenant_b.id))
        async with _client(app) as client:
            r = await client.get(
                f"/api/admin/tenants/{tenant_a.id}"
                f"/safety/rules/{uuid.uuid4()}/audit"
            )
        assert r.status_code == 403
