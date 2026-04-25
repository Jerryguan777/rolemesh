"""safety_rules_audit — rule-change audit trail (reflection #8).

Approval has this, safety didn't. Compliance scenario: "Jan 3 admin
disabled the SSN rule, leak followed on Jan 4" must be reconstructable
from DB alone. These tests pin:

  - create → one audit row, action='created', after_state populated
  - update → one audit row per semantic change, before/after captured
  - delete → one audit row, before_state captured, rule_id preserved
    even though the row itself is gone
  - actor_user_id propagates via the safety.actor_user_id GUC
  - no-op update (same field values) does NOT write an audit row —
    updated_at moves are noise, not compliance events
  - cross-tenant list query never leaks another tenant's history
  - rule_id is NOT FK'd to safety_rules, so the audit trail survives
    hard-delete of the rule itself — regression test for that
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.db import pg

pytestmark = pytest.mark.usefixtures("test_db")


async def _seed_tenant_and_user() -> tuple[str, str, str]:
    t = await pg.create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await pg.create_user(
        tenant_id=t.id, name="Admin", email="a@x.com", role="admin"
    )
    cw = await pg.create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    return t.id, u.id, cw.id


class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_create_writes_created_row_with_actor(self) -> None:
        tid, uid, _ = await _seed_tenant_and_user()
        rule = await pg.create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            actor_user_id=uid,
        )
        rows = await pg.list_safety_rules_audit(tenant_id=tid)
        assert len(rows) == 1
        assert rows[0]["action"] == "created"
        assert rows[0]["rule_id"] == rule.id
        assert rows[0]["actor_user_id"] == uid
        assert rows[0]["before_state"] is None
        assert rows[0]["after_state"] is not None
        assert rows[0]["after_state"]["check_id"] == "pii.regex"

    @pytest.mark.asyncio
    async def test_create_with_null_actor_allowed(self) -> None:
        # Bulk-import / migration scripts have no human actor. The
        # audit row still exists but carries NULL actor_user_id.
        tid, _, _ = await _seed_tenant_and_user()
        await pg.create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={},
        )
        rows = await pg.list_safety_rules_audit(tenant_id=tid)
        assert rows[0]["actor_user_id"] is None

    @pytest.mark.asyncio
    async def test_update_writes_update_row_with_before_after(self) -> None:
        tid, uid, _ = await _seed_tenant_and_user()
        rule = await pg.create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            enabled=True,
            actor_user_id=uid,
        )
        await pg.update_safety_rule(
            rule.id, tenant_id=tid, enabled=False, actor_user_id=uid
        )
        rows = await pg.list_safety_rules_audit(
            tenant_id=tid, rule_id=rule.id
        )
        # Newest first: updated, then created.
        assert rows[0]["action"] == "updated"
        assert rows[0]["before_state"]["enabled"] is True
        assert rows[0]["after_state"]["enabled"] is False
        assert rows[0]["actor_user_id"] == uid

    @pytest.mark.asyncio
    async def test_noop_update_writes_no_audit_row(self) -> None:
        # The trigger's IF v_before <> v_after guard filters out
        # UPDATEs where no semantic field changed. updated_at moving
        # alone must not pollute the compliance timeline.
        tid, uid, _ = await _seed_tenant_and_user()
        rule = await pg.create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={},
            enabled=True,
            actor_user_id=uid,
        )
        # Touch only with unchanged fields. update_safety_rule returns
        # the current row when nothing was passed — but even explicit
        # same-value assignments should be filtered.
        await pg.update_safety_rule(
            rule.id, tenant_id=tid, enabled=True, actor_user_id=uid
        )
        rows = await pg.list_safety_rules_audit(
            tenant_id=tid, rule_id=rule.id
        )
        assert len(rows) == 1  # only the 'created' row
        assert rows[0]["action"] == "created"

    @pytest.mark.asyncio
    async def test_delete_writes_deleted_row_with_before_state(self) -> None:
        tid, uid, _ = await _seed_tenant_and_user()
        rule = await pg.create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
            description="blocks PII",
            actor_user_id=uid,
        )
        assert (
            await pg.delete_safety_rule(rule.id, tenant_id=tid, actor_user_id=uid)
            is True
        )
        rows = await pg.list_safety_rules_audit(
            tenant_id=tid, rule_id=rule.id
        )
        assert rows[0]["action"] == "deleted"
        assert rows[0]["before_state"]["description"] == "blocks PII"
        assert rows[0]["after_state"] is None
        assert rows[0]["actor_user_id"] == uid
        # The rule row is gone, but the audit row remains.
        assert await pg.get_safety_rule(rule.id, tenant_id=tid) is None

    @pytest.mark.asyncio
    async def test_audit_survives_rule_hard_delete(self) -> None:
        # Reflection #8 explicitly calls out that compliance requires
        # the timeline to survive even when a rule is hard-deleted.
        # The audit row has rule_id but no FK, so DELETE CASCADE from
        # safety_rules would never orphan it.
        tid, uid, _ = await _seed_tenant_and_user()
        rule = await pg.create_safety_rule(
            tenant_id=tid,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={},
            actor_user_id=uid,
        )
        await pg.delete_safety_rule(rule.id, tenant_id=tid, actor_user_id=uid)
        rows = await pg.list_safety_rules_audit(tenant_id=tid)
        rule_ids_in_audit = {r["rule_id"] for r in rows}
        assert rule.id in rule_ids_in_audit


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_list_never_leaks(self) -> None:
        tid_a, uid_a, _ = await _seed_tenant_and_user()
        tid_b, _uid_b, _ = await _seed_tenant_and_user()
        await pg.create_safety_rule(
            tenant_id=tid_a,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={},
            actor_user_id=uid_a,
        )
        # Tenant B must see zero rows even though tenant A has one.
        rows_b = await pg.list_safety_rules_audit(tenant_id=tid_b)
        assert rows_b == []


class TestRestApiWritesAudit:
    """The REST admin path sets actor_user_id automatically. This
    test goes through the HTTP boundary so a refactor that forgets
    to pass user.user_id into the CRUD layer surfaces here.
    """

    @pytest.mark.asyncio
    async def test_rest_create_attributes_audit_to_caller(self) -> None:
        import httpx
        from fastapi import FastAPI

        from rolemesh.auth.provider import AuthenticatedUser
        from webui import admin
        from webui.dependencies import (
            get_current_user,
            require_manage_agents,
            require_manage_tenant,
            require_manage_users,
        )

        tid, uid, _ = await _seed_tenant_and_user()
        authed = AuthenticatedUser(
            user_id=uid, tenant_id=tid, role="admin",
            email="a@x.com", name="Admin",
        )
        app = FastAPI()
        app.include_router(admin.router)

        async def _return_user() -> AuthenticatedUser:
            return authed

        app.dependency_overrides[get_current_user] = _return_user
        app.dependency_overrides[require_manage_agents] = _return_user
        app.dependency_overrides[require_manage_tenant] = _return_user
        app.dependency_overrides[require_manage_users] = _return_user

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                },
            )
            assert r.status_code == 201

        rows = await pg.list_safety_rules_audit(tenant_id=tid)
        assert len(rows) == 1
        assert rows[0]["actor_user_id"] == uid, (
            "REST admin path must thread user.user_id through to the "
            "CRUD layer's actor_user_id argument"
        )
