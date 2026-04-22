"""Full-chain E2E starting from the admin REST surface.

The existing test_pii_block.py starts at pg.create_safety_rule,
skipping the admin REST layer. That leaves the most common production
flow — a tenant admin POSTs a rule → browses GET /rules → the agent
container picks it up → tool call is blocked → both decision-audit
AND rule-change-audit are written — without a single test covering
it end-to-end.

This file adds that coverage. It uses httpx.AsyncClient with
ASGITransport to call the real FastAPI app, so the REST layer's
pydantic validation, tenant-scoping, and actor attribution all
participate. The only thing mocked is NATS transport — the subscriber
is invoked directly on bytes, same pattern as test_pii_block.py.

Key assertions beyond the existing happy path:

  1. POST -> 201 creates a rule AND emits a safety_rules_audit row
     with action='created' attributed to the calling user.
  2. GET /rules returns the newly-created rule for the owning tenant
     but NOT for another tenant (cross-tenant listing isolation).
  3. The snapshot returned by list_safety_rules_for_coworker is
     structurally the same list the container would run against —
     verified by running it through an actual SafetyHookHandler.
  4. PATCH /rules/{id} emits a safety_rules_audit row with
     action='updated', before_state and after_state populated, AND
     the change propagates through a fresh snapshot to a new Handler.
  5. DELETE /rules/{id} emits action='deleted' audit row AND the rule
     is gone from list_safety_rules_for_coworker.

These make the RuleLifecycle + AuditTrail pair observable through
the same HTTP-to-DB chain operators actually use.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_container_registry
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import pg
from rolemesh.safety.engine import SafetyEngine
from rolemesh.safety.subscriber import (
    SafetyEventsSubscriber,
    TrustedCoworker,
)
from webui import admin
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeToolCtx:
    tenant_id: str
    coworker_id: str
    job_id: str = "job-rest-e2e"
    conversation_id: str = "conv-rest-e2e"
    user_id: str = "user-rest-e2e"
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))


@dataclass(frozen=True)
class _TrustedRec:
    tenant_id: str
    id: str


def _build_app(user: AuthenticatedUser) -> FastAPI:
    app = FastAPI()
    app.include_router(admin.router)

    async def _return_user() -> AuthenticatedUser:
        return user

    # Override all the auth dependencies so the test focuses on the
    # safety layer, not OIDC/role plumbing.
    app.dependency_overrides[get_current_user] = _return_user
    app.dependency_overrides[require_manage_agents] = _return_user
    app.dependency_overrides[require_manage_tenant] = _return_user
    app.dependency_overrides[require_manage_users] = _return_user
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


async def _seed_tenant_user_coworker() -> tuple[str, str, str, str]:
    """Returns (tenant_id, user_id, coworker_id, coworker_folder)."""
    tenant = await pg.create_tenant(
        name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
    )
    user = await pg.create_user(
        tenant_id=tenant.id, name="Admin",
        email=f"a-{uuid.uuid4().hex[:8]}@x.com", role="owner",
    )
    cw = await pg.create_coworker(
        tenant_id=tenant.id, name="cw",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    return tenant.id, user.id, cw.id, cw.folder


def _authed_user(tenant_id: str, user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role="owner",
        email="a@x.com", name="Admin",
    )


async def _run_ssn_through_container(
    snapshot: list[dict[str, Any]],
    tenant_id: str,
    cw_id: str,
) -> tuple[SafetyHookHandler, _FakeToolCtx, Any]:
    """Simulate container: build Handler from snapshot, fire SSN tool call."""
    tool_ctx = _FakeToolCtx(tenant_id=tenant_id, coworker_id=cw_id)
    handler = SafetyHookHandler(
        rules=snapshot,
        registry=build_container_registry(),
        tool_ctx=tool_ctx,  # type: ignore[arg-type]
    )
    verdict = await handler.on_pre_tool_use(
        ToolCallEvent(
            tool_name="github__create_issue",
            tool_input={"body": "my SSN is 123-45-6789"},
        )
    )
    return handler, tool_ctx, verdict


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRestCreateToAudit:
    @pytest.mark.asyncio
    async def test_post_then_container_block_then_audit_trail(
        self,
    ) -> None:
        """POST /rules → container picks up snapshot → blocks →
        decision audit + rule-change audit both populated."""
        tid, uid, cw_id, _ = await _seed_tenant_user_coworker()
        app = _build_app(_authed_user(tid, uid))

        # 1. Admin creates rule via HTTP. This is the path a real
        #    operator takes — pydantic config validation, tenant
        #    scoping, and actor attribution all happen here.
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "description": "block SSN in tool calls",
                },
            )
            assert r.status_code == 201, r.text
            rule_id = r.json()["id"]

            # Same HTTP surface: GET /rules must surface the rule we
            # just created. A cross-tenant leak would be caught by
            # TestCrossTenantListing below.
            r = await client.get("/api/admin/safety/rules")
            assert r.status_code == 200
            assert any(row["id"] == rule_id for row in r.json())

        # 2. Container side: loads snapshot exactly as
        #    container_executor would. If the REST layer quietly
        #    changed its persistence shape (e.g. a field rename), the
        #    snapshot shape the Handler consumes would break here.
        snapshot = [
            r.to_snapshot_dict()
            for r in await pg.list_safety_rules_for_coworker(tid, cw_id)
        ]
        assert snapshot, "POST result must be visible to the container"

        # 3. Run the full container path: Handler blocks, audit event
        #    published via fake ToolContext.
        _handler, tool_ctx, verdict = await _run_ssn_through_container(
            snapshot, tid, cw_id
        )
        assert verdict is not None and verdict.block
        assert tool_ctx.events, "Handler must publish an audit event"

        # 4. Subscriber handles the audit event with trust check.
        #    Using the real SafetyEventsSubscriber means a refactor
        #    that broke the tenant check would surface here, not
        #    just in isolated subscriber tests.
        def _lookup(cid: str) -> TrustedCoworker | None:
            if cid == cw_id:
                return _TrustedRec(tenant_id=tid, id=cw_id)
            return None

        subscriber = SafetyEventsSubscriber(
            engine=SafetyEngine(), coworker_lookup=_lookup
        )
        _, event_payload = tool_ctx.events[0]
        await subscriber.on_message_bytes(
            json.dumps(event_payload).encode()
        )

        # 5a. Decision audit: safety_decisions has the block row.
        decisions = await pg.list_safety_decisions(tid)
        assert len(decisions) == 1
        assert decisions[0]["verdict_action"] == "block"
        assert decisions[0]["triggered_rule_ids"] == [rule_id]

        # 5b. Rule-change audit: safety_rules_audit has the 'created'
        #     row attributed to the caller. This is the timeline
        #     operators need for "who set this rule?" forensics.
        rule_audit = await pg.list_safety_rules_audit(
            tenant_id=tid, rule_id=rule_id
        )
        assert len(rule_audit) == 1
        assert rule_audit[0]["action"] == "created"
        assert rule_audit[0]["actor_user_id"] == uid, (
            "REST create must attribute the audit row to the caller; "
            "a regression here breaks compliance attribution"
        )


class TestRestPatchPropagates:
    @pytest.mark.asyncio
    async def test_patch_enabled_false_propagates_to_new_container(
        self,
    ) -> None:
        """PATCH enabled=false via HTTP → fresh snapshot → new Handler
        allows the same SSN that a pre-PATCH Handler would have
        blocked. Also asserts an 'updated' rule-audit row with the
        before/after state captured."""
        tid, uid, cw_id, _ = await _seed_tenant_user_coworker()
        app = _build_app(_authed_user(tid, uid))

        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                },
            )
            rule_id = r.json()["id"]

            # Baseline: Handler built from current snapshot blocks.
            snap_before = [
                r.to_snapshot_dict()
                for r in await pg.list_safety_rules_for_coworker(
                    tid, cw_id
                )
            ]
            _, _, v_before = await _run_ssn_through_container(
                snap_before, tid, cw_id
            )
            assert v_before is not None and v_before.block

            # PATCH through HTTP, not direct pg.update.
            r = await client.patch(
                f"/api/admin/safety/rules/{rule_id}",
                json={"enabled": False},
            )
            assert r.status_code == 200
            assert r.json()["enabled"] is False

            # Fresh snapshot reflects the change. New Handler allows.
            snap_after = [
                r.to_snapshot_dict()
                for r in await pg.list_safety_rules_for_coworker(
                    tid, cw_id
                )
            ]
            assert snap_after == [], (
                "PATCH enabled=false MUST remove the rule from the "
                "container-bound snapshot"
            )
            _, _, v_after = await _run_ssn_through_container(
                snap_after, tid, cw_id
            )
            assert v_after is None, (
                "PATCH enabled=false MUST let SSN pass on the next "
                "container start — this is the rule-toggle contract"
            )

        # Audit: 'updated' row with before=enabled:true,
        # after=enabled:false.
        rule_audit = await pg.list_safety_rules_audit(
            tenant_id=tid, rule_id=rule_id
        )
        # Newest first: updated then created.
        assert [r["action"] for r in rule_audit] == ["updated", "created"]
        updated_row = rule_audit[0]
        assert updated_row["before_state"]["enabled"] is True
        assert updated_row["after_state"]["enabled"] is False
        assert updated_row["actor_user_id"] == uid


class TestRestDeleteAudit:
    @pytest.mark.asyncio
    async def test_delete_removes_rule_and_emits_deleted_audit(
        self,
    ) -> None:
        tid, uid, cw_id, _ = await _seed_tenant_user_coworker()
        app = _build_app(_authed_user(tid, uid))

        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {"patterns": {"SSN": True}},
                    "description": "SSN blocker",
                },
            )
            rule_id = r.json()["id"]

            r = await client.delete(
                f"/api/admin/safety/rules/{rule_id}"
            )
            assert r.status_code == 204

        # Rule is gone from container-visible queries.
        snap = await pg.list_safety_rules_for_coworker(tid, cw_id)
        assert snap == []

        # But the audit row survives — the compliance timeline
        # outlives the rule itself.
        rule_audit = await pg.list_safety_rules_audit(
            tenant_id=tid, rule_id=rule_id
        )
        actions = [r["action"] for r in rule_audit]
        assert actions == ["deleted", "created"]
        deleted = rule_audit[0]
        assert deleted["before_state"]["description"] == "SSN blocker"
        assert deleted["after_state"] is None
        assert deleted["actor_user_id"] == uid


class TestCrossTenantListing:
    @pytest.mark.asyncio
    async def test_tenant_b_cannot_see_tenant_a_rule(self) -> None:
        """Two tenants, each with their own rule. GET /rules for
        tenant B MUST NOT return tenant A's rule. Covered at the
        test_api.py level separately; re-verified here through the
        full HTTP path so an E2E reader can confirm the isolation
        without hopping suites."""
        tid_a, uid_a, _, _ = await _seed_tenant_user_coworker()
        tid_b, uid_b, _, _ = await _seed_tenant_user_coworker()

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
            created_id = r.json()["id"]

            # B's list must be empty.
            r = await c_b.get("/api/admin/safety/rules")
            assert r.status_code == 200
            assert r.json() == []

            # B's direct GET by id must 404 (not 403 — avoids
            # existence leakage).
            r = await c_b.get(
                f"/api/admin/safety/rules/{created_id}"
            )
            assert r.status_code == 404
