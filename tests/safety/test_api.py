"""REST API tests for /api/admin/safety/rules endpoints.

Mirrors tests/approval/test_api.py structure. Focuses on:
  - CRUD happy path (create + list + get + patch + delete)
  - server-side validation: unknown check_id, unsupported stage,
    malformed config all reject with 400
  - cross-tenant isolation: a tenant admin cannot read/modify another
    tenant's rules (404, not 403, to avoid leaking existence)
  - cross-tenant coworker_id rejection on create (404 via
    _get_agent_or_404)
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db.pg import (
    create_coworker,
    create_tenant,
    create_user,
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


async def _seed() -> tuple[str, str, str]:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(
        tenant_id=t.id, name="Alice", email="a@x.com", role="owner"
    )
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    return t.id, u.id, cw.id


def _authed_user(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role="owner",
        email="a@x.com", name="Alice",
    )


class TestCreateRule:
    @pytest.mark.asyncio
    async def test_tenant_wide_rule(self) -> None:
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "description": "block SSN",
                },
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["tenant_id"] == tid
            assert body["coworker_id"] is None
            assert body["stage"] == "pre_tool_call"
            assert body["description"] == "block SSN"

    @pytest.mark.asyncio
    async def test_coworker_scoped_rule(self) -> None:
        tid, uid, cwid = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"EMAIL": True}},
                    "coworker_id": cwid,
                },
            )
            assert r.status_code == 201, r.text
            assert r.json()["coworker_id"] == cwid

    @pytest.mark.asyncio
    async def test_unknown_check_id_400(self) -> None:
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "does.not.exist",
                    "config": {},
                },
            )
            assert r.status_code == 400
            assert "Unknown safety check_id" in r.text

    @pytest.mark.asyncio
    async def test_invalid_stage_422(self) -> None:
        # Stage values that don't match the regex should be rejected by
        # Pydantic before hitting the handler — that's a 422 per
        # FastAPI conventions.
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "invented_stage",
                    "check_id": "pii.regex",
                    "config": {},
                },
            )
            assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_pattern_key_400(self) -> None:
        # Previously, {"patterns": {"SNN": true}} was silently ignored
        # in the container — admins saw "rule created" then no actual
        # detection. pydantic config_model now rejects at REST time.
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SNN": True}},  # typo
                },
            )
            assert r.status_code == 400
            assert "Unknown PII pattern" in r.text

    @pytest.mark.asyncio
    async def test_non_bool_pattern_value_400(self) -> None:
        # "yes" and "no" are both truthy strings — the previous code
        # silently accepted them, enabling the pattern regardless of
        # intent. pydantic config_model now rejects non-bool values.
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": "yes"}},
                },
            )
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_extra_config_key_400(self) -> None:
        # extra="forbid" rejects anything outside PIIRegexConfig.
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {
                        "patterns": {"SSN": True},
                        "extra_field": "should-fail",
                    },
                },
            )
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_stage_outside_check_support_400(self) -> None:
        # pii.regex does not declare PRE_COMPACTION in its stages set.
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_compaction",
                    "check_id": "pii.regex",
                    "config": {},
                },
            )
            assert r.status_code == 400
            assert "does not support stage" in r.text

    @pytest.mark.asyncio
    async def test_cross_tenant_coworker_404(self) -> None:
        # coworker_id from another tenant must 404 to avoid leaking
        # whether the coworker exists.
        tid_a, uid_a, _ = await _seed()
        _tid_b, _uid_b, cwid_b = await _seed()
        app = _build_app(_authed_user(tid_a, uid_a))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "coworker_id": cwid_b,
                },
            )
            assert r.status_code == 404


class TestListAndGet:
    @pytest.mark.asyncio
    async def test_list_returns_tenant_rules_only(self) -> None:
        tid_a, uid_a, _ = await _seed()
        tid_b, uid_b, _ = await _seed()
        app_a = _build_app(_authed_user(tid_a, uid_a))
        app_b = _build_app(_authed_user(tid_b, uid_b))
        async with _client(app_a) as c_a, _client(app_b) as c_b:
            r = await c_a.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                },
            )
            assert r.status_code == 201
            # Tenant B must not see A's rule.
            r = await c_b.get("/api/admin/safety/rules")
            assert r.status_code == 200
            assert r.json() == []
            # Tenant A sees its own rule.
            r = await c_a.get("/api/admin/safety/rules")
            assert len(r.json()) == 1

    @pytest.mark.asyncio
    async def test_get_other_tenants_rule_404(self) -> None:
        tid_a, uid_a, _ = await _seed()
        tid_b, uid_b, _ = await _seed()
        app_a = _build_app(_authed_user(tid_a, uid_a))
        app_b = _build_app(_authed_user(tid_b, uid_b))
        async with _client(app_a) as c_a, _client(app_b) as c_b:
            r = await c_a.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                },
            )
            rule_id = r.json()["id"]
            # B should get 404, not 403, to avoid existence leak.
            r = await c_b.get(f"/api/admin/safety/rules/{rule_id}")
            assert r.status_code == 404


class TestPatchAndDelete:
    @pytest.mark.asyncio
    async def test_enable_toggle(self) -> None:
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "enabled": True,
                },
            )
            rule_id = r.json()["id"]
            r = await client.patch(
                f"/api/admin/safety/rules/{rule_id}",
                json={"enabled": False},
            )
            assert r.status_code == 200
            assert r.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_patch_cross_tenant_404(self) -> None:
        tid_a, uid_a, _ = await _seed()
        tid_b, uid_b, _ = await _seed()
        app_a = _build_app(_authed_user(tid_a, uid_a))
        app_b = _build_app(_authed_user(tid_b, uid_b))
        async with _client(app_a) as c_a, _client(app_b) as c_b:
            r = await c_a.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {},
                },
            )
            rule_id = r.json()["id"]
            r = await c_b.patch(
                f"/api/admin/safety/rules/{rule_id}",
                json={"enabled": False},
            )
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_check_id_revalidates_stage(self) -> None:
        # If PATCH changes check_id, the stage-compatibility check must
        # rerun so an admin can't orphan a stage → check combo.
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {},
                },
            )
            rule_id = r.json()["id"]
            # Change to a fictional check_id — must 400.
            r = await client.patch(
                f"/api/admin/safety/rules/{rule_id}",
                json={"check_id": "does.not.exist"},
            )
            assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        tid, uid, _ = await _seed()
        app = _build_app(_authed_user(tid, uid))
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {},
                },
            )
            rule_id = r.json()["id"]
            r = await client.delete(f"/api/admin/safety/rules/{rule_id}")
            assert r.status_code == 204
            r = await client.get(f"/api/admin/safety/rules/{rule_id}")
            assert r.status_code == 404
