"""Tests for ``CoworkerState.from_coworker``.

The contract: ``cs.config`` must be the same ``Coworker`` instance the
caller passed in, so any field added to ``Coworker`` is automatically
reachable via ``cs.config.<field>`` without a projection step.
"""

from __future__ import annotations

from rolemesh.core.orchestrator_state import CoworkerState
from rolemesh.core.types import Coworker


def test_from_coworker_preserves_db_row_identity() -> None:
    cw = Coworker(
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        name="trader",
        folder="trader",
        agent_backend="claude",
        system_prompt="You trade.",
        max_concurrent=2,
        status="active",
        created_at="2024-01-01T00:00:00Z",
        agent_role="agent",
    )
    cs = CoworkerState.from_coworker(cw)

    # cs.config must be the same instance, not a field-by-field copy —
    # this is what makes the projection drift-proof.
    assert cs.config is cw

    assert cs.config.status == "active"
    assert cs.config.created_at == "2024-01-01T00:00:00Z"
    assert cs.config.container_config is None

    # Coworker.__post_init__ fills permissions from agent_role when None.
    assert cs.config.permissions is not None

    # Conversations / bindings start empty.
    assert cs.conversations == {}
    assert cs.channel_bindings == {}


def test_from_coworker_trigger_pattern_is_case_insensitive() -> None:
    cw = Coworker(
        id="cw1", tenant_id="t1", name="Trader", folder="trader",
    )
    cs = CoworkerState.from_coworker(cw)

    assert cs.trigger_pattern.search("ping @TRADER")
    assert cs.trigger_pattern.search("ping @trader")
    assert cs.trigger_pattern.search("ping @Trader")
    assert not cs.trigger_pattern.search("ping @other")
