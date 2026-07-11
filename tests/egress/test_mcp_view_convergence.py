"""Convergence contract for every materialized view of ``mcp_servers``.

A row in ``mcp_servers`` (name / url / headers / auth_mode) has, at the
time of writing, exactly THREE runtime materializations:

  1. gateway-registry        — ``reverse_proxy._mcp_registry`` in the
                               gateway process; fed by
                               ``mcp_cache.subscribe_mcp_changes``.
  2. orch-registry-mirror    — the same module-level dict in the
                               orchestrator process (source for the
                               gateway's boot snapshot RPC); fed by a
                               second subscription to the same subject.
  3. coworker-projection     — ``CoworkerState.mcp_configs`` (the JOIN
                               snapshot handed to agents at spawn); fed
                               by ``refresh_coworkers_for_mcp_server``
                               via the ``egress.mcp.changed``
                               subscription wired in ``rolemesh.main``.

REGISTRATION REQUIREMENT: if you add a NEW materialized view of
``mcp_servers`` anywhere in the codebase, you MUST add a case to this
file proving it converges after a change event. This file is the CI
form of the view inventory — before it existed the inventory lived in
scattered comments, and the coworker-projection leg was silently missed
by the ``/mcp-servers`` update path (agents kept spawning with stale
URLs until an unrelated unlink/relink; observed in production).

Views 1 and 2 share ``apply_change_event`` — both cases run the same
code path on purpose: the parametrization documents the inventory, not
code-path diversity.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.core.types import Coworker, McpServerConfig
from rolemesh.egress import reverse_proxy
from rolemesh.egress.mcp_cache import apply_change_event
from rolemesh.egress.reverse_proxy import get_mcp_registry, register_mcp_server
from rolemesh.orchestration.coworker_hot_reload import (
    refresh_coworkers_for_mcp_server,
)

OLD_URL = "http://mcp-x:8000"
NEW_URL = "http://mcp-x-moved:9000"

CHANGE_EVENT = {
    "action": "updated",
    "name": "X",
    "url": NEW_URL,
    "headers": {"X-Key": "v2"},
    "auth_mode": "service",
}


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    # Same convention as test_mcp_cache.py: the registry is a
    # module-global dict; wipe it between cases.
    reverse_proxy._mcp_registry.clear()


def _apply_to_registry() -> None:
    register_mcp_server("X", OLD_URL, {"X-Key": "v1"}, "user")
    apply_change_event(CHANGE_EVENT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "view",
    ["gateway-registry", "orch-registry-mirror", "coworker-projection"],
)
async def test_view_converges_after_mcp_server_update(view: str) -> None:
    if view in ("gateway-registry", "orch-registry-mirror"):
        _apply_to_registry()
        url, headers, auth_mode = get_mcp_registry()["X"]
        assert url == NEW_URL
        assert headers == {"X-Key": "v2"}
        assert auth_mode == "service"
        return

    # coworker-projection: the refresh re-reads DB truth (faked here)
    # instead of trusting event fields — the event's url is origin-only.
    cw = Coworker(
        id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
        name="cw",
        folder="cw-folder",
        agent_backend="claude",
        system_prompt="hi",
    )
    state = OrchestratorState()
    state.coworkers[cw.id] = CoworkerState(
        config=cw,
        mcp_configs=[McpServerConfig(name="X", type="http", url=OLD_URL)],
    )

    async def _fetch(coworker_id: str, tenant_id: str) -> list[McpServerConfig]:
        assert (coworker_id, tenant_id) == (cw.id, cw.tenant_id)
        return [
            McpServerConfig(
                name="X", type="http", url=NEW_URL,
                headers={"X-Key": "v2"}, auth_mode="service",
            )
        ]

    n = await refresh_coworkers_for_mcp_server(
        name=CHANGE_EVENT["name"],  # type: ignore[arg-type]
        state=state,
        fetch_mcp_configs=_fetch,
    )

    assert n == 1
    (cfg,) = state.coworkers[cw.id].mcp_configs
    assert cfg.url == NEW_URL
    assert cfg.headers == {"X-Key": "v2"}
    assert cfg.auth_mode == "service"
