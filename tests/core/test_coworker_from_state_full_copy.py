"""Guard ``_coworker_from_state`` against the post-PR-#27 lossy-copy regression.

Handbook §3 fact #22 + §6 Step 2.3 — the pre-Phase-A implementation
preserved only 8 of 13 ``Coworker`` fields and silently reset the
rest. After Phase A the function is ``return cw_state.config`` and
must round-trip every field including the two new ones
(``is_frontdesk`` / ``routing_description``).

This test is intentionally an *adversarial* full-field assertion:
each populated field is something the lossy version dropped, plus the
new Phase A fields. If a future change reintroduces a partial copy,
each missing field surfaces as a distinct test failure.
"""

from __future__ import annotations

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.orchestrator_state import CoworkerState, build_trigger_pattern
from rolemesh.core.types import (
    AdditionalMount,
    ContainerConfig,
    Coworker,
    McpServerConfig,
)
from rolemesh.main import _coworker_from_state


def _populated_coworker() -> Coworker:
    """Build a Coworker with every non-default field populated."""
    return Coworker(
        id="00000000-0000-0000-0000-000000000001",
        tenant_id="00000000-0000-0000-0000-000000000002",
        name="Frontdesk",
        folder="frontdesk-prod",
        agent_backend="pi",
        system_prompt="You are the frontdesk.",
        tools=[
            McpServerConfig(
                name="market-data",
                type="sse",
                url="http://localhost:9100/mcp/",
                headers={"x-api-key": "secret"},
                auth_mode="service",
                tool_reversibility={"query": True, "place_order": False},
            ),
        ],
        container_config=ContainerConfig(
            additional_mounts=[
                AdditionalMount(host_path="/srv/data", container_path="/data", readonly=False),
            ],
            timeout=600_000,
            runtime="runsc",
            memory_limit="1g",
            cpu_limit=2.0,
        ),
        max_concurrent=8,
        status="paused",
        created_at="2026-05-20T12:34:56+00:00",
        agent_role="super_agent",
        permissions=AgentPermissions(
            data_scope="tenant",
            task_schedule=True,
            task_manage_others=True,
            agent_delegate=True,
        ),
        is_frontdesk=True,
        routing_description="(blank for frontdesks)",
    )


def test_coworker_from_state_returns_config_identity() -> None:
    cw = _populated_coworker()
    cs = CoworkerState(config=cw, trigger_pattern=build_trigger_pattern(cw.name))

    result = _coworker_from_state(cs)

    # `return cw_state.config` is the identity post-fix; this catches
    # any reintroduction of an intermediate Coworker(...) constructor.
    assert result is cw


def test_every_field_round_trips() -> None:
    """Adversarial per-field assertion. If one field silently resets,
    the failure pinpoints exactly which.
    """
    cw = _populated_coworker()
    cs = CoworkerState(config=cw, trigger_pattern=build_trigger_pattern(cw.name))

    r = _coworker_from_state(cs)

    assert r.id == cw.id
    assert r.tenant_id == cw.tenant_id
    assert r.name == cw.name
    assert r.folder == cw.folder
    assert r.agent_backend == cw.agent_backend
    assert r.system_prompt == cw.system_prompt
    assert r.tools == cw.tools
    assert r.container_config == cw.container_config
    assert r.max_concurrent == cw.max_concurrent
    assert r.status == cw.status
    assert r.created_at == cw.created_at
    assert r.agent_role == cw.agent_role
    assert r.permissions == cw.permissions
    assert r.is_frontdesk == cw.is_frontdesk
    assert r.routing_description == cw.routing_description


def test_lossy_partial_copy_would_have_dropped_these_fields() -> None:
    """Pin the contract that motivated the fix: any of these reverting
    to the dataclass default means a partial copy snuck back in.
    """
    cw = _populated_coworker()
    cs = CoworkerState(config=cw, trigger_pattern=build_trigger_pattern(cw.name))

    r = _coworker_from_state(cs)

    defaults_that_lossy_copy_returned = (
        ("status", "active"),
        ("agent_role", "agent"),
        ("created_at", ""),
        ("container_config", None),
        ("is_frontdesk", False),
        ("routing_description", None),
    )
    for field_name, default_value in defaults_that_lossy_copy_returned:
        assert getattr(r, field_name) != default_value, (
            f"_coworker_from_state appears to be returning the dataclass "
            f"default for {field_name!r} (lossy partial-copy regression). "
            f"Expected the populated value from cw_state.config."
        )

    # Permissions: the lossy version reset to role-default (AgentPermissions
    # for the synthesized agent_role='agent', which has agent_delegate=False).
    assert r.permissions is not None
    assert r.permissions.agent_delegate is True, (
        "Permissions look reset to role-defaults — partial copy regression."
    )
