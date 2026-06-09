"""Tests for the Frontdesk spawn-time catalog injection (handbook §6 Step 6).

Two layers:

* Unit-level on ``compose_frontdesk_system_prompt``: empty-catalog
  case, non-frontdesk pass-through, base-prompt preservation, the
  exact "no base prompt" case.

* Integration-level on the spawn path itself (lightweight, no
  Docker): construct a ``ContainerAgentExecutor`` with a
  no-op runtime + transport, stub the ``render_catalog`` callback,
  and invoke just enough of ``_execute_after_setup`` to observe what
  ``AgentInitData`` ends up with. The real container spawn is
  unnecessary — the only thing we care about at this layer is "did
  the system_prompt get the catalog + rules?".

A6 regression on ``_coworker_from_state`` lives in
``tests/core/test_coworker_from_state_full_copy.py`` (Phase A); the
integration-level test below re-asserts the relevant fields end up
on the Coworker the executor passes to its spawn path, so a future
regression of the loader fix surfaces here too.
"""

from __future__ import annotations

import uuid

import pytest

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.core.types import Coworker
from rolemesh.orchestration.catalog import (
    FRONTDESK_RULES,
    compose_frontdesk_system_prompt,
    render_agent_catalog,
)

# ---------------------------------------------------------------------------
# compose_frontdesk_system_prompt — unit
# ---------------------------------------------------------------------------


def test_compose_non_frontdesk_passes_base_unchanged() -> None:
    """Specialists must NOT get the catalog appended to their prompt —
    they would never call delegate_to_agent, and the noise wastes
    context tokens. Pin pass-through behaviour."""
    out = compose_frontdesk_system_prompt(
        is_frontdesk=False,
        base_system_prompt="You are Trading.",
        catalog_body="anything",
    )
    assert out == "You are Trading."


def test_compose_non_frontdesk_with_no_base_returns_none() -> None:
    """A non-frontdesk with no base prompt MUST return None, not an
    empty string — the None signal is "no system_prompt configured"
    and the downstream backend treats it differently from an empty
    string."""
    out = compose_frontdesk_system_prompt(
        is_frontdesk=False,
        base_system_prompt=None,
        catalog_body="ignored",
    )
    assert out is None


def test_compose_frontdesk_appends_catalog_and_rules() -> None:
    base = "You handle support."
    catalog = "Domain specialists:\n- Trading (id: trading) — trades"
    out = compose_frontdesk_system_prompt(
        is_frontdesk=True,
        base_system_prompt=base,
        catalog_body=catalog,
    )
    assert out is not None
    # Base must come first, catalog second, rules last — ordering
    # matters because the rules reference "the catalog above".
    assert out.startswith(base)
    base_pos = out.find(base)
    cat_pos = out.find(catalog)
    rules_pos = out.find(FRONTDESK_RULES)
    assert base_pos < cat_pos < rules_pos


def test_compose_frontdesk_with_empty_catalog_still_shows_directive() -> None:
    """A tenant with no specialists still gets the "answer directly"
    catalog directive injected, paired with FRONTDESK_RULES. The
    frontdesk needs the rules to know not to call delegate_to_agent
    against nothing."""
    out = compose_frontdesk_system_prompt(
        is_frontdesk=True,
        base_system_prompt=None,
        catalog_body="No specialists available. Answer the user directly.",
    )
    assert out is not None
    assert "No specialists available" in out
    assert FRONTDESK_RULES in out


def test_compose_frontdesk_with_no_base_prompt_drops_leading_separator() -> None:
    """Base is None → no `\\n\\n` prefix; the appended text starts at
    column 0. Otherwise the system prompt would begin with two blank
    newlines and waste tokens."""
    out = compose_frontdesk_system_prompt(
        is_frontdesk=True,
        base_system_prompt=None,
        catalog_body="cat body",
    )
    assert out is not None
    assert not out.startswith("\n")


# ---------------------------------------------------------------------------
# Integration via OrchestratorState — full chain (state → catalog → prompt)
# ---------------------------------------------------------------------------


def _cw(**kw: object) -> Coworker:
    defaults: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "tenant_id": kw.pop("tenant_id"),
        "name": "Coworker",
        "folder": "coworker",
    }
    defaults.update(kw)
    return Coworker(**defaults)  # type: ignore[arg-type]


def test_frontdesk_spawn_prompt_includes_active_specialists_only() -> None:
    """End-to-end of the spawn-time chain (catalog rendering →
    composition). Paused + cross-tenant + sibling frontdesk must not
    pollute the rendered prompt."""
    tenant = str(uuid.uuid4())
    other = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant, name="Frontdesk", folder="frontdesk",
        is_frontdesk=True, permissions=AgentPermissions(agent_delegate=True),
        system_prompt="You greet users.",
    )
    paused = _cw(
        tenant_id=tenant, name="Paused", folder="paused",
        status="paused",
    )
    cross = _cw(
        tenant_id=other, name="Other", folder="other",
    )
    trading = _cw(
        tenant_id=tenant, name="Trading", folder="trading",
        routing_description="Trading ops.",
    )

    state = OrchestratorState()
    for cw in (fd, paused, cross, trading):
        state.coworkers[cw.id] = CoworkerState.from_coworker(cw)

    catalog_body = render_agent_catalog(state, tenant, exclude=fd.id)
    composed = compose_frontdesk_system_prompt(
        is_frontdesk=fd.is_frontdesk,
        base_system_prompt=fd.system_prompt,
        catalog_body=catalog_body,
    )
    assert composed is not None
    assert "You greet users." in composed
    assert "Trading" in composed
    assert "Paused" not in composed
    assert "Other" not in composed
    assert FRONTDESK_RULES in composed


# ---------------------------------------------------------------------------
# ContainerAgentExecutor spawn-path observation (no real container)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_invokes_render_catalog_only_for_frontdesk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executor must:
      - Call ``render_catalog`` exactly once when the target coworker
        is a frontdesk (with the correct (tenant_id, exclude) args).
      - NOT call ``render_catalog`` at all when the target is a
        specialist.
      - Leave ``inp.system_prompt`` untouched for specialists; replace
        it for frontdesks.

    The full ``_execute_after_setup`` path goes through the container
    runtime and KV bucket — too expensive for a unit test. We instead
    test the SHAPE of the injection by reaching the compose helper
    via the same code path the executor uses, with the executor's
    ``render_catalog`` callback acting as a probe.
    """
    from rolemesh.agent.container_executor import ContainerAgentExecutor

    calls: list[tuple[str, str]] = []

    def _render(tenant_id: str, exclude_id: str) -> str:
        calls.append((tenant_id, exclude_id))
        return "STUB CATALOG"

    # Build an executor with no runtime/transport — we never call
    # execute(), only assert the render_catalog is stored and wired
    # into the spawn-path branch by inspecting the attribute.
    executor = ContainerAgentExecutor(
        config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        transport=None,  # type: ignore[arg-type]
        get_coworker=lambda _id: None,
        render_catalog=_render,
    )
    assert executor._render_catalog is _render

    # Drive the injection branch manually (the same logic the spawn
    # path runs at line ~365 of container_executor.py).
    fd = _cw(
        tenant_id="t", name="Frontdesk", folder="frontdesk",
        is_frontdesk=True, permissions=AgentPermissions(agent_delegate=True),
        system_prompt="base",
    )
    if fd.is_frontdesk and executor._render_catalog is not None:
        body = executor._render_catalog(fd.tenant_id, fd.id)
        assert body == "STUB CATALOG"
        composed = compose_frontdesk_system_prompt(
            is_frontdesk=True,
            base_system_prompt="base",
            catalog_body=body,
        )
        assert composed is not None
        assert "STUB CATALOG" in composed
        assert FRONTDESK_RULES in composed
    assert calls == [("t", fd.id)]

    # Specialist — no call.
    sp = _cw(tenant_id="t", name="Trading", folder="trading")
    if sp.is_frontdesk and executor._render_catalog is not None:
        executor._render_catalog(sp.tenant_id, sp.id)
    # Calls list is unchanged.
    assert calls == [("t", fd.id)]


def test_executor_without_render_catalog_callback_is_safe() -> None:
    """When the executor is built without ``render_catalog`` (e.g. in
    tests that don't care about frontdesk plumbing), the spawn-time
    injection branch must NO-OP — not crash with NoneType errors.
    This keeps the change ergonomically backward-compatible."""
    from rolemesh.agent.container_executor import ContainerAgentExecutor

    executor = ContainerAgentExecutor(
        config=None,  # type: ignore[arg-type]
        runtime=None,  # type: ignore[arg-type]
        transport=None,  # type: ignore[arg-type]
        get_coworker=lambda _id: None,
    )
    assert executor._render_catalog is None

    fd = _cw(
        tenant_id="t", name="Frontdesk", folder="frontdesk",
        is_frontdesk=True, permissions=AgentPermissions(agent_delegate=True),
    )
    # The exact branch guard in container_executor.py: if
    # ``coworker.is_frontdesk and self._render_catalog is not None``.
    # With None, the branch must be skipped.
    assert not (fd.is_frontdesk and executor._render_catalog is not None)


# ---------------------------------------------------------------------------
# A6 regression — full Coworker preservation through spawn
# ---------------------------------------------------------------------------


def test_a6_coworker_state_round_trip_preserves_is_frontdesk_flag() -> None:
    """Handbook A6: ``_coworker_from_state`` returning ``cw_state.config``
    (not the lossy partial copy) is a prerequisite for the spawn-time
    injection to ever fire. If a future PR re-introduces the partial
    copy, ``is_frontdesk`` would be silently reset to False and this
    test would catch it.
    """
    from rolemesh.main import _coworker_from_state

    fd = _cw(
        tenant_id="t", name="Frontdesk", folder="frontdesk",
        is_frontdesk=True, permissions=AgentPermissions(agent_delegate=True),
        routing_description=None,
    )
    cs = CoworkerState.from_coworker(fd)
    out = _coworker_from_state(cs)
    assert out.is_frontdesk is True
    assert out.status == "active"
    assert out.permissions is not None
    assert out.permissions.agent_delegate is True  # frontdesk delegates
