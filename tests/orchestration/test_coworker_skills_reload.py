"""Unit tests for the per-tenant skills hot-reload helper.

Counterpart to ``test_v1_coworkers_crud.test_reload_coworker_into_state_*``.
Skips the live NATS round-trip; the publisher / JetStream subscription
seam is already exercised by ``test_v1_skills.py`` (event count) and
``test_v1_coworkers_crud.py`` (round-trip wiring).
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.core.orchestrator_state import (
    CoworkerState,
    OrchestratorState,
)
from rolemesh.core.types import Coworker, Skill
from rolemesh.orchestration.coworker_hot_reload import (
    reload_coworker_skills_into_state,
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


def _make_skill(name: str, *, tenant_id: str) -> Skill:
    return Skill(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=name,
        enabled=True,
        files={},
    )


@pytest.mark.asyncio
async def test_reload_skills_replaces_projection_on_existing_state() -> None:
    """The reloader mutates ``CoworkerState.skills`` in place; the
    runtime ``conversations`` / ``channel_bindings`` dicts are not
    touched. Replacing the state whole would orphan those — the
    in-flight message loop holds references to them.
    """
    cw = _make_coworker()
    state = OrchestratorState()
    state.coworkers[cw.id] = CoworkerState(
        config=cw,
        skills=[_make_skill("old", tenant_id=cw.tenant_id)],
    )
    state.coworkers[cw.id].conversations["sentinel"] = "kept"  # type: ignore[assignment]

    fresh = [
        _make_skill("alpha", tenant_id=cw.tenant_id),
        _make_skill("beta", tenant_id=cw.tenant_id),
    ]

    async def _fetch(coworker_id: str, tenant_id: str) -> list[Skill]:
        # Anti-mirror: assertion on inputs, not just on outputs. A
        # bug that flips the call order to ``(tenant_id, coworker_id)``
        # fails here directly.
        assert coworker_id == cw.id
        assert tenant_id == cw.tenant_id
        return fresh

    ok = await reload_coworker_skills_into_state(
        coworker_id=cw.id,
        tenant_id=cw.tenant_id,
        state=state,
        fetch_skills=_fetch,
    )
    assert ok is True
    cached = state.coworkers[cw.id]
    assert [s.name for s in cached.skills] == ["alpha", "beta"]
    assert cached.conversations.get("sentinel") == "kept"


@pytest.mark.asyncio
async def test_reload_skills_returns_false_when_coworker_not_in_state() -> None:
    """The skills subscriber should not fabricate a fresh
    ``CoworkerState`` from a ``skills_changed`` event — only the
    ``restart`` path creates new entries. A missed restart leaves
    the cache empty; the next message-routed-at-coworker reads
    through the DB anyway.
    """
    state = OrchestratorState()

    async def _fetch(_cid: str, _tid: str) -> list[Skill]:
        # If we get here the test should fail — the reloader should
        # bail out before invoking fetch_skills.
        raise AssertionError("fetch_skills should not be called")

    ok = await reload_coworker_skills_into_state(
        coworker_id=str(uuid.uuid4()),
        tenant_id=str(uuid.uuid4()),
        state=state,
        fetch_skills=_fetch,
    )
    assert ok is False
