"""REST-layer validation for V2 P0.4 reversibility guard and P1.1
action_override whitelist.

The runtime guard in pipeline_core is authoritative at execution time,
but catching misconfigurations at admin create / patch time gives
operators immediate feedback. These tests pin the REST rejection
contracts so a later refactor can't silently drop them.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.types import McpServerConfig
from rolemesh.db import (
    create_coworker,
    create_tenant,
    create_user,
)
from rolemesh.safety.registry import (
    build_orchestrator_registry,
    get_orchestrator_registry,
    reset_orchestrator_registry,
)
from rolemesh.safety.types import CostClass, SafetyContext, Stage, Verdict
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


class _StubSlowCheck:
    """Minimal slow check registered into the orchestrator registry
    just for the duration of reversibility-guard tests.
    """

    id = "stub.slow.pretool"
    version = "1"
    stages = frozenset({Stage.PRE_TOOL_CALL})
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset({"STUB"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, object]
    ) -> Verdict:
        return Verdict(action="allow")


@pytest.fixture
def slow_check_registered() -> None:
    # Replace the process-wide registry with a copy containing our
    # slow stub — without disturbing the default cheap checks so the
    # cross-check validations (stage compatibility etc.) still work.
    reset_orchestrator_registry()
    base = build_orchestrator_registry()
    base.register(_StubSlowCheck())
    from rolemesh.safety import registry as reg_mod

    reg_mod._ORCHESTRATOR_REGISTRY = base  # type: ignore[attr-defined]
    assert get_orchestrator_registry().has("stub.slow.pretool")
    yield
    reset_orchestrator_registry()


async def _seed_tenant_with_user(
    role: str = "owner",
) -> tuple[AuthenticatedUser, str]:
    tenant = await create_tenant(
        name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
    )
    actor = await create_user(
        tenant_id=tenant.id,
        name="admin",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        role=role,
    )
    user = AuthenticatedUser(
        user_id=actor.id,
        tenant_id=tenant.id,
        role=role,
        email=actor.email,
    )
    return user, tenant.id


class TestReversibilityGuardRest:
    @pytest.mark.asyncio
    async def test_slow_pretool_rejected_when_scope_has_reversible_tool(
        self,
        slow_check_registered: None,
    ) -> None:
        user, tenant_id = await _seed_tenant_with_user()
        # Coworker with an MCP server declaring a reversible tool.
        cw = await create_coworker(
            tenant_id=tenant_id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
            tools=[
                McpServerConfig(
                    name="github",
                    type="http",
                    url="http://localhost:9100/mcp/",
                    tool_reversibility={
                        "list_pulls": True,  # read-only, reversible
                        "create_pr": False,
                    },
                )
            ],
        )
        app = _build_app(user)
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "stub.slow.pretool",
                    "config": {},
                    "coworker_id": cw.id,
                },
            )
        assert r.status_code == 400
        detail = r.json()["detail"]
        # Error surface must identify the offending tool + budget so
        # the operator knows how to fix the rule.
        assert "stub.slow.pretool" in detail
        assert "list_pulls" in detail
        assert "100 ms" in detail

    @pytest.mark.asyncio
    async def test_slow_pretool_accepted_when_scope_has_no_reversible_tools(
        self,
        slow_check_registered: None,
    ) -> None:
        user, tenant_id = await _seed_tenant_with_user()
        cw = await create_coworker(
            tenant_id=tenant_id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
            tools=[
                McpServerConfig(
                    name="github",
                    type="http",
                    url="http://localhost:9100/mcp/",
                    tool_reversibility={
                        "create_pr": False,
                        "merge_pr": False,
                    },
                )
            ],
        )
        app = _build_app(user)
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "stub.slow.pretool",
                    "config": {},
                    "coworker_id": cw.id,
                },
            )
        assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_slow_model_output_accepted_regardless_of_reversibility(
        self,
        slow_check_registered: None,
    ) -> None:
        # Guard is scoped to PRE_TOOL_CALL. A slow check on MODEL_OUTPUT
        # (which supports it via a wider frozenset) would have a 1000 ms
        # budget, so reversibility does not enter the decision.
        user, tenant_id = await _seed_tenant_with_user()
        cw = await create_coworker(
            tenant_id=tenant_id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
            tools=[
                McpServerConfig(
                    name="github",
                    type="http",
                    url="http://localhost:9100/mcp/",
                    tool_reversibility={"list_pulls": True},
                )
            ],
        )

        # Register a slow check that ALSO supports MODEL_OUTPUT.
        class _ModelOutputSlow:
            id = "stub.slow.model"
            version = "1"
            stages = frozenset({Stage.MODEL_OUTPUT})
            cost_class: CostClass = "slow"
            supported_codes: frozenset[str] = frozenset({"STUB"})
            config_model = None

            async def check(
                self, _ctx: SafetyContext, _config: dict[str, object]
            ) -> Verdict:
                return Verdict(action="allow")

        get_orchestrator_registry().register(_ModelOutputSlow())

        app = _build_app(user)
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "model_output",
                    "check_id": "stub.slow.model",
                    "config": {},
                    "coworker_id": cw.id,
                },
            )
        assert r.status_code == 201


class TestActionOverrideRest:
    @pytest.mark.asyncio
    async def test_valid_override_accepted(self) -> None:
        user, _ = await _seed_tenant_with_user()
        app = _build_app(user)
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {
                        "patterns": {"SSN": True},
                        "action_override": "require_approval",
                    },
                },
            )
        assert r.status_code == 201

    @pytest.mark.asyncio
    async def test_redact_override_rejected(self) -> None:
        user, _ = await _seed_tenant_with_user()
        app = _build_app(user)
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {
                        "patterns": {"SSN": True},
                        "action_override": "redact",
                    },
                },
            )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "redact" in detail.lower()

    @pytest.mark.asyncio
    async def test_unknown_override_rejected(self) -> None:
        user, _ = await _seed_tenant_with_user()
        app = _build_app(user)
        async with _client(app) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "pii.regex",
                    "config": {
                        "patterns": {"SSN": True},
                        "action_override": "teleport",
                    },
                },
            )
        assert r.status_code == 400
