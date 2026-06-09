"""Pin the Frontdesk catalog/FRONTDESK_RULES naming contract.

Handbook §4 #16, §6 Step 4.4, §8 #26.

The frontdesk inherits broad bash permissions. Whenever the
catalog or the rules templates use words that suggest filesystem
semantics ("folder", "directory"), real models have been observed to
``ls`` instead of calling the ``delegate_to_agent`` tool. The single
observable surface that controls this behaviour is what these two
constants render — so the contract is pinned at the rendering site,
not at any one downstream test.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.core.types import Coworker
from rolemesh.orchestration.catalog import FRONTDESK_RULES, render_agent_catalog


def _cw(**kw: object) -> Coworker:
    """Build a minimal Coworker with sensible defaults; let __post_init__
    fill ``permissions`` so we get the role-appropriate defaults from
    auth.permissions (rather than hand-rolling the perms shape here)."""
    defaults: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "tenant_id": kw.pop("tenant_id"),  # required
        "name": "Coworker",
        "folder": "coworker",
    }
    defaults.update(kw)
    return Coworker(**defaults)  # type: ignore[arg-type]


def _seed(state: OrchestratorState, *cws: Coworker) -> None:
    for cw in cws:
        state.coworkers[cw.id] = CoworkerState.from_coworker(cw)


def test_catalog_renders_id_label_not_folder_label() -> None:
    state = OrchestratorState()
    tenant = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant,
        name="Frontdesk",
        folder="frontdesk",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    tr = _cw(
        tenant_id=tenant,
        name="Trading",
        folder="trading",
        routing_description="Place trades and check positions.",
    )
    _seed(state, fd, tr)

    catalog = render_agent_catalog(state, tenant, exclude=fd.id)

    lower = catalog.lower()
    assert "folder" not in lower, catalog
    assert "directory" not in lower, catalog
    assert "(id:" in catalog, catalog
    # Body contents: name visible, slug rendered after id:.
    assert "Trading" in catalog
    assert "(id: trading)" in catalog
    assert "Place trades and check positions." in catalog


def test_frontdesk_rules_avoid_filesystem_semantics() -> None:
    lower = FRONTDESK_RULES.lower()
    assert "folder slug" not in lower, FRONTDESK_RULES
    assert "directory" not in lower, FRONTDESK_RULES
    # Failure-passthrough contract (handbook §4 #23): the rules MUST
    # tell the LLM to include the specialist name AND literal reason
    # on isError=true. Pin the exact phrasing — if it drifts, the
    # routing-accuracy eval cases that target this contract will
    # silently stop matching.
    assert "MUST include both the specialist's name" in FRONTDESK_RULES
    # Anti filesystem-semantics line.
    assert "NOT files, directories" in FRONTDESK_RULES
    # Pin the agent-id-not-path terminology so a future copy-edit
    # doesn't reintroduce the "folder slug" framing.
    assert "agent id" in FRONTDESK_RULES.lower()


def test_catalog_filters_inactive_frontdesk_self_and_cross_tenant() -> None:
    state = OrchestratorState()
    tenant = str(uuid.uuid4())
    other_tenant = str(uuid.uuid4())

    fd = _cw(
        tenant_id=tenant,
        name="Frontdesk",
        folder="frontdesk",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    paused = _cw(
        tenant_id=tenant,
        name="Paused",
        folder="paused",
        status="paused",
    )
    cross = _cw(
        tenant_id=other_tenant,
        name="Cross",
        folder="cross",
    )
    other_fd = _cw(
        tenant_id=tenant,
        name="OtherFrontdesk",
        folder="other-fd",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    active = _cw(
        tenant_id=tenant,
        name="Trading",
        folder="trading",
    )
    _seed(state, fd, paused, cross, other_fd, active)

    catalog = render_agent_catalog(state, tenant, exclude=fd.id)

    assert "Trading" in catalog
    assert "Paused" not in catalog
    assert "Cross" not in catalog
    assert "OtherFrontdesk" not in catalog
    assert "Frontdesk" not in catalog


def test_catalog_self_exclusion_takes_precedence() -> None:
    """A caller asking for the catalog must never see itself even if it
    nominally passes the other filters. Important because future PRs
    might relax the ``is_frontdesk`` filter; ``exclude`` is the
    last-line guarantee against self-routing loops surfacing in the
    catalog."""
    state = OrchestratorState()
    tenant = str(uuid.uuid4())
    a = _cw(
        tenant_id=tenant,
        name="DomainA",
        folder="a",
        is_frontdesk=False,
    )
    b = _cw(
        tenant_id=tenant,
        name="DomainB",
        folder="b",
        is_frontdesk=False,
    )
    _seed(state, a, b)

    catalog = render_agent_catalog(state, tenant, exclude=a.id)

    assert "DomainA" not in catalog
    assert "DomainB" in catalog


def test_catalog_empty_when_no_active_specialists() -> None:
    state = OrchestratorState()
    tenant = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant,
        name="Frontdesk",
        folder="frontdesk",
        is_frontdesk=True,
        permissions=AgentPermissions(agent_delegate=True),
    )
    _seed(state, fd)
    catalog = render_agent_catalog(state, tenant, exclude=fd.id)
    assert catalog == "No specialists available. Answer the user directly."


def test_catalog_placeholder_when_routing_description_missing() -> None:
    state = OrchestratorState()
    tenant = str(uuid.uuid4())
    tr = _cw(
        tenant_id=tenant,
        name="Trading",
        folder="trading",
        routing_description=None,
    )
    _seed(state, tr)
    catalog = render_agent_catalog(state, tenant, exclude="never-matches")
    assert "(no description provided)" in catalog


def test_routing_description_drop_is_caught_by_catalog_assertion() -> None:
    """Mutation-style probe: if a future PR drops ``routing_description``
    from ``Coworker``/CoworkerState the catalog stops carrying it, which
    is the visible regression we'd want to catch. We simulate the drop by
    setting it to None on a copy and verifying the placeholder shows up
    in the rendered catalog."""
    state = OrchestratorState()
    tenant = str(uuid.uuid4())
    cw = _cw(
        tenant_id=tenant,
        name="Trading",
        folder="trading",
        routing_description="real description",
    )
    _seed(state, cw)
    full = render_agent_catalog(state, tenant, exclude="x")
    assert "real description" in full

    # Drop the description and re-render in place.
    dropped = replace(cw, routing_description=None)
    state.coworkers[cw.id] = CoworkerState.from_coworker(dropped)
    after = render_agent_catalog(state, tenant, exclude="x")
    assert "real description" not in after
    assert "(no description provided)" in after
