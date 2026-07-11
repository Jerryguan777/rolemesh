"""Unit tests for ``refresh_coworkers_for_mcp_server`` and its event seam.

Covers the third leg of the ``egress.mcp.changed`` fan-out: a
``/mcp-servers`` PATCH/DELETE must refresh the ``CoworkerState.
mcp_configs`` projection of every bound coworker, without relying on a
later unlink/relink. The NATS wiring lives in ``rolemesh.main``; these
tests exercise the pure logic with injected fakes.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.core.types import Coworker, McpServerConfig
from rolemesh.orchestration.coworker_hot_reload import (
    apply_mcp_changed_event_to_projections,
    refresh_coworkers_for_mcp_server,
)


def _make_coworker(*, tenant_id: str | None = None) -> Coworker:
    return Coworker(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id or str(uuid.uuid4()),
        name="cw",
        folder="cw-folder",
        agent_backend="claude",
        system_prompt="hi",
    )


def _mcp(name: str, url: str = "http://old:1") -> McpServerConfig:
    return McpServerConfig(name=name, type="http", url=url)


def _state_with(*coworkers: tuple[Coworker, list[McpServerConfig]]) -> OrchestratorState:
    state = OrchestratorState()
    for cw, configs in coworkers:
        state.coworkers[cw.id] = CoworkerState(config=cw, mcp_configs=list(configs))
    return state


class _FetchRecorder:
    """Fake ``list_coworker_mcp_configs`` that records call args."""

    def __init__(self, result: list[McpServerConfig]) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, coworker_id: str, tenant_id: str) -> list[McpServerConfig]:
        self.calls.append((coworker_id, tenant_id))
        return list(self.result)


@pytest.mark.asyncio
async def test_updated_refreshes_bound_coworkers_across_tenants() -> None:
    """Two coworkers in different tenants bound to X refresh (fetch is
    called with each coworker's own id + tenant); an unrelated third
    coworker is left untouched."""
    cw_a = _make_coworker()
    cw_b = _make_coworker()  # different tenant (random uuid)
    cw_other = _make_coworker()
    state = _state_with(
        (cw_a, [_mcp("X")]),
        (cw_b, [_mcp("X"), _mcp("other")]),
        (cw_other, [_mcp("unrelated")]),
    )
    fetch = _FetchRecorder([_mcp("X", url="http://new:9")])

    n = await refresh_coworkers_for_mcp_server(
        name="X", state=state, fetch_mcp_configs=fetch,
    )

    assert n == 2
    assert sorted(fetch.calls) == sorted(
        [(cw_a.id, cw_a.tenant_id), (cw_b.id, cw_b.tenant_id)]
    )
    assert state.coworkers[cw_a.id].mcp_configs[0].url == "http://new:9"
    assert state.coworkers[cw_b.id].mcp_configs[0].url == "http://new:9"
    # The unrelated coworker's projection was not re-fetched or altered.
    assert state.coworkers[cw_other.id].mcp_configs[0].name == "unrelated"


@pytest.mark.asyncio
async def test_deleted_row_matches_via_stale_projection_scan() -> None:
    """After a DELETE the row is gone, so a DB reverse lookup finds
    nothing — the in-memory scan must still match the stale projection
    and refresh it (the re-read then drops the vanished server).

    Note the API refuses DELETE while coworkers are still bound (409
    RESOURCE_IN_USE), so this path is defensive: it covers the race
    between the reference check and the DELETE, and non-API deletes.
    """
    cw = _make_coworker()
    state = _state_with((cw, [_mcp("X")]))
    fetch = _FetchRecorder([])  # server gone from the JOIN

    async def _lookup_finds_nothing(name: str) -> list[tuple[str, str]]:
        return []

    n = await refresh_coworkers_for_mcp_server(
        name="X",
        state=state,
        fetch_mcp_configs=fetch,
        list_bound_coworker_ids=_lookup_finds_nothing,
    )

    assert n == 1
    assert state.coworkers[cw.id].mcp_configs == []


@pytest.mark.asyncio
async def test_rename_found_via_db_reverse_lookup() -> None:
    """THE reason the DB reverse lookup exists: a PATCH rename means
    the event carries the NEW name while cached projections hold the
    OLD one, so the in-memory scan alone finds nobody. The junction
    binds by server id, so the lookup (by new name) still returns the
    bound coworkers. Do not "simplify" discovery back to scan-only —
    this test is the guard against exactly that."""
    cw = _make_coworker()
    state = _state_with((cw, [_mcp("old-name")]))
    fetch = _FetchRecorder([_mcp("new-name", url="http://new:9")])

    async def _lookup(name: str) -> list[tuple[str, str]]:
        assert name == "new-name"
        return [(cw.id, cw.tenant_id)]

    n = await refresh_coworkers_for_mcp_server(
        name="new-name",
        state=state,
        fetch_mcp_configs=fetch,
        list_bound_coworker_ids=_lookup,
    )

    assert n == 1
    assert state.coworkers[cw.id].mcp_configs[0].name == "new-name"


@pytest.mark.asyncio
async def test_one_failing_coworker_does_not_starve_the_rest() -> None:
    cw_ok = _make_coworker()
    cw_bad = _make_coworker()
    state = _state_with((cw_ok, [_mcp("X")]), (cw_bad, [_mcp("X")]))

    calls: list[str] = []

    async def _fetch(coworker_id: str, tenant_id: str) -> list[McpServerConfig]:
        calls.append(coworker_id)
        if coworker_id == cw_bad.id:
            raise RuntimeError("db blip")
        return [_mcp("X", url="http://new:9")]

    n = await refresh_coworkers_for_mcp_server(
        name="X", state=state, fetch_mcp_configs=_fetch,
    )

    assert n == 1  # only the healthy coworker counts
    assert set(calls) == {cw_ok.id, cw_bad.id}  # both were attempted
    assert state.coworkers[cw_ok.id].mcp_configs[0].url == "http://new:9"


@pytest.mark.asyncio
async def test_failed_db_lookup_degrades_to_scan_only() -> None:
    """A broken reverse lookup must not stop the refreshes the
    in-memory scan can still deliver."""
    cw = _make_coworker()
    state = _state_with((cw, [_mcp("X")]))
    fetch = _FetchRecorder([_mcp("X", url="http://new:9")])

    async def _lookup_raises(name: str) -> list[tuple[str, str]]:
        raise RuntimeError("db down")

    n = await refresh_coworkers_for_mcp_server(
        name="X",
        state=state,
        fetch_mcp_configs=fetch,
        list_bound_coworker_ids=_lookup_raises,
    )

    assert n == 1
    assert state.coworkers[cw.id].mcp_configs[0].url == "http://new:9"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        "not-a-dict",
        ["also", "not", "a", "dict"],
        {},
        {"action": "updated"},  # missing name
        {"action": "updated", "name": ""},  # empty name
        {"action": "updated", "name": 42},  # non-str name
    ],
)
async def test_malformed_event_is_dropped_without_refreshing(event: object) -> None:
    """Robustness contract mirrors ``mcp_cache.apply_change_event``."""
    cw = _make_coworker()
    state = _state_with((cw, [_mcp("X")]))
    fetch = _FetchRecorder([_mcp("X", url="http://new:9")])

    n = await apply_mcp_changed_event_to_projections(
        event, state=state, fetch_mcp_configs=fetch,
    )

    assert n == 0
    assert fetch.calls == []
    assert state.coworkers[cw.id].mcp_configs[0].url == "http://old:1"


@pytest.mark.asyncio
async def test_created_event_refreshes_nothing() -> None:
    """A just-created server cannot be bound to anyone yet: the scan
    matches nobody and the reverse lookup returns no junction rows, so
    no per-action branch is needed."""
    cw = _make_coworker()
    state = _state_with((cw, [_mcp("existing")]))
    fetch = _FetchRecorder([])

    async def _lookup(name: str) -> list[tuple[str, str]]:
        return []

    n = await apply_mcp_changed_event_to_projections(
        {"action": "created", "name": "brand-new", "url": "http://n:1",
         "headers": {}, "auth_mode": "user"},
        state=state,
        fetch_mcp_configs=fetch,
        list_bound_coworker_ids=_lookup,
    )

    assert n == 0
    assert fetch.calls == []
