"""End-to-end reversibility round-trip.

The tool reversibility signal threads through five distinct layers:

  1. ``McpServerConfig.tool_reversibility`` on the coworker config
     (admin creates via REST / DB seeder)
  2. PostgreSQL ``coworkers.tools`` JSONB column (serialize on
     create/update, deserialize on read)
  3. ``container_executor`` copies into ``McpServerSpec.tool_reversibility``
     when building ``AgentInitData``
  4. NATS KV / agent-init transport via ``AgentInitData.serialize``
     → ``AgentInitData.deserialize``
  5. ``ToolContext.mcp_tool_reversibility`` flat map in the container,
     consumed by ``get_tool_reversibility`` at hook time

Each leg has a unit test, but the chain has no E2E. A silent drop on
any layer would let slow checks run at PRE_TOOL_CALL against
reversible tools — exceeding the 100 ms budget guard that P0.4
promised. This test follows the entire chain with real DB reads.

Also covers the REST-time guard (from P0.4): a slow check + PRE_TOOL_CALL
rule scoped to a coworker whose tools include a reversible entry must
be rejected at admin create time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.core.types import McpServerConfig
from rolemesh.db import pg
from rolemesh.ipc.protocol import AgentInitData, McpServerSpec
from rolemesh.safety.registry import (
    build_orchestrator_registry,
    get_orchestrator_registry,
    reset_orchestrator_registry,
)
from rolemesh.safety.tool_reversibility import (
    BUILTIN_REVERSIBILITY,
    resolve_from_full_tool_name,
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


@dataclass
class _ToolCtxStub:
    """ToolContext shape needed by get_tool_reversibility — mirrors the
    subset consumed downstream. Uses the real
    ``rolemesh.safety.tool_reversibility.resolve_from_full_tool_name``
    so a regression in the resolver shows up here too.
    """

    mcp_tool_reversibility: dict[str, dict[str, bool]]

    def get_tool_reversibility(self, tool_name: str) -> bool:
        return resolve_from_full_tool_name(
            tool_name, self.mcp_tool_reversibility
        )


class TestPersistenceRoundTrip:
    @pytest.mark.asyncio
    async def test_tool_reversibility_survives_all_five_layers(
        self,
    ) -> None:
        """End-to-end persistence: create a coworker with a mixed
        reversibility map, read it back, build AgentInitData,
        serialize → deserialize, build the ToolContext map, resolve
        each tool via the same helper the hook handler uses.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        mcp = McpServerConfig(
            name="github",
            type="http",
            url="http://localhost:9100/mcp/",
            tool_reversibility={
                "list_pulls": True,       # read-only
                "create_pr": False,       # side-effecting
                "merge_pr": False,
                # A typo / extra key that isn't a real tool — the
                # resolver should just never match against it, not
                # bubble through as True for any name.
                "legacy_typo": True,
            },
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
            tools=[mcp],
        )

        # Layer 2 → 3 → 4: read coworker back, build spec, serialize.
        fetched = await pg.get_coworker(cw.id)
        assert fetched is not None
        assert len(fetched.tools) == 1
        persisted_mcp = fetched.tools[0]
        # Persisted map must match what we inserted (no silent drop
        # on the JSONB round-trip).
        assert persisted_mcp.tool_reversibility == {
            "list_pulls": True,
            "create_pr": False,
            "merge_pr": False,
            "legacy_typo": True,
        }

        spec = McpServerSpec(
            name=persisted_mcp.name,
            type=persisted_mcp.type,
            url="http://proxy/mcp-proxy/github/mcp/",
            tool_reversibility=dict(persisted_mcp.tool_reversibility),
        )
        init = AgentInitData(
            prompt="",
            group_folder=cw.folder,
            chat_jid="chat",
            tenant_id=tenant.id,
            coworker_id=cw.id,
            mcp_servers=[spec],
        )

        # Layer 4 round-trip: JSON on the wire.
        decoded = AgentInitData.deserialize(init.serialize())
        assert decoded.mcp_servers is not None
        decoded_spec = decoded.mcp_servers[0]
        assert decoded_spec.tool_reversibility == {
            "list_pulls": True,
            "create_pr": False,
            "merge_pr": False,
            "legacy_typo": True,
        }

        # Layer 5: container-side ToolContext flatten + resolve.
        mcp_map: dict[str, dict[str, bool]] = {}
        for s in decoded.mcp_servers:
            if s.tool_reversibility:
                mcp_map[s.name] = dict(s.tool_reversibility)
        ctx = _ToolCtxStub(mcp_tool_reversibility=mcp_map)

        # MCP tools resolve via the per-server map.
        assert ctx.get_tool_reversibility("mcp__github__list_pulls") is True
        assert ctx.get_tool_reversibility("mcp__github__create_pr") is False
        assert ctx.get_tool_reversibility("mcp__github__merge_pr") is False

        # Stock Claude tools still hit the builtin table; the MCP
        # map must NOT override them (operators can't claim Write
        # is reversible by stuffing it in their MCP overrides).
        assert ctx.get_tool_reversibility("Read") is True
        assert ctx.get_tool_reversibility("Write") is False
        assert ctx.get_tool_reversibility("Bash") is False

        # Unknown tool → fail-safe False. The "legacy_typo": True
        # entry must NOT match any tool whose name happens to equal
        # that key via the mcp-prefix split.
        assert ctx.get_tool_reversibility("mcp__github__unknown") is False

        # Sanity: our built-in snapshot is still sensible. If this
        # ever becomes an empty dict the whole reversibility path
        # collapses silently.
        assert len(BUILTIN_REVERSIBILITY) >= 10

    @pytest.mark.asyncio
    async def test_update_coworker_preserves_tool_reversibility(
        self,
    ) -> None:
        """PATCH path: updating a coworker's ``tools`` list must
        round-trip the reversibility map too. A regression that
        forgot to include the key in the UPDATE serialiser would
        silently drop overrides on any edit.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
            tools=[
                McpServerConfig(
                    name="api",
                    type="http",
                    url="http://localhost:9100/mcp/",
                    tool_reversibility={"read": True, "write": False},
                )
            ],
        )
        # Update with a modified reversibility map.
        await pg.update_coworker(
            cw.id,
            tools=[
                McpServerConfig(
                    name="api",
                    type="http",
                    url="http://localhost:9100/mcp/",
                    tool_reversibility={
                        "read": True,
                        "write": False,
                        "delete": False,  # new entry
                    },
                )
            ],
        )
        fetched = await pg.get_coworker(cw.id)
        assert fetched is not None
        assert fetched.tools[0].tool_reversibility == {
            "read": True,
            "write": False,
            "delete": False,
        }


# ---------------------------------------------------------------------------
# REST-layer reversibility guard (P0.4 admin-time rejection)
# ---------------------------------------------------------------------------


class _StubSlowPretool:
    id = "stub.slow.pretool"
    version = "1"
    stages = frozenset({Stage.PRE_TOOL_CALL})
    cost_class: CostClass = "slow"
    supported_codes: frozenset[str] = frozenset({"STUB"})
    config_model = None

    async def check(
        self, _ctx: SafetyContext, _config: dict[str, Any]
    ) -> Verdict:
        return Verdict(action="allow")


@pytest.fixture
def slow_check_registered() -> Any:
    """Install a stub slow check into the orchestrator registry for
    the duration of these REST tests — the real default registry
    has no slow check at PRE_TOOL_CALL (all shipped slow checks
    declare other stages), so the reversibility guard would have
    nothing to fire on."""
    reset_orchestrator_registry()
    base = build_orchestrator_registry()
    base.register(_StubSlowPretool())
    from rolemesh.safety import registry as reg_mod

    reg_mod._ORCHESTRATOR_REGISTRY = base  # type: ignore[attr-defined]
    assert get_orchestrator_registry().has("stub.slow.pretool")
    yield
    reset_orchestrator_registry()


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


async def _seed_user(tenant_id: str) -> AuthenticatedUser:
    user = await pg.create_user(
        tenant_id=tenant_id,
        name="Admin",
        email=f"admin-{uuid.uuid4().hex[:6]}@example.com",
        role="owner",
    )
    return AuthenticatedUser(
        user_id=user.id,
        tenant_id=tenant_id,
        role="owner",
        email=user.email,
    )


class TestRestGuardWithPersistedReversibility:
    @pytest.mark.asyncio
    async def test_rest_rejects_slow_pretool_rule_when_persisted_tool_is_reversible(
        self, slow_check_registered: None
    ) -> None:
        """Full chain: coworker has a reversible tool in its
        persisted config → admin POSTs a slow-check rule scoped to
        that coworker at PRE_TOOL_CALL → REST guard refuses with
        400. The guard has to (a) read the coworker from DB, (b)
        walk the tools JSONB, (c) resolve reversibility via the
        shared helper. Persisting the reversibility map wrong in
        step 2→3 of the chain would make this test fail with 201
        (rule accepted incorrectly).
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        user = await _seed_user(tenant.id)
        cw = await pg.create_coworker(
            tenant_id=tenant.id,
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
        app = _build_app(user)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
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
        assert "stub.slow.pretool" in detail
        assert "list_pulls" in detail
        assert "100 ms" in detail

    @pytest.mark.asyncio
    async def test_rest_accepts_slow_pretool_when_all_tools_are_irreversible(
        self, slow_check_registered: None
    ) -> None:
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        user = await _seed_user(tenant.id)
        cw = await pg.create_coworker(
            tenant_id=tenant.id,
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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r = await client.post(
                "/api/admin/safety/rules",
                json={
                    "stage": "pre_tool_call",
                    "check_id": "stub.slow.pretool",
                    "config": {},
                    "coworker_id": cw.id,
                },
            )
        assert r.status_code == 201, r.text
