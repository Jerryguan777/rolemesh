"""Regression: the admin safety-rule write schema must accept every engine stage.

Field bug: PR #50 added ``EGRESS_REQUEST`` to the engine ``Stage`` enum, the v1
read schema, and each check's ``stages`` — but the admin-write validator kept a
hardcoded 5-stage regex (``_SAFETY_STAGE_PATTERN``). Editing an
``egress.domain_rule`` rule (whose stage is ``egress_request``) was therefore
rejected with::

    HTTP 422 string_pattern_mismatch  loc=["body","stage"]  input="egress_request"

The pattern is now *derived* from the canonical ``SafetyStage`` literal. These
tests pin the specific regression and guard against the pattern drifting out of
sync with the engine ``Stage`` set ever again.
"""

from __future__ import annotations

import pytest

from rolemesh.safety.types import Stage
from webui.schemas import (
    _SAFETY_STAGE_PATTERN,
    SafetyRuleCreate,
    SafetyRuleUpdate,
)

ALL_STAGES = [s.value for s in Stage]


@pytest.mark.parametrize("stage", ALL_STAGES)
def test_admin_create_accepts_every_engine_stage(stage: str) -> None:
    assert SafetyRuleCreate(stage=stage, check_id="x").stage == stage


@pytest.mark.parametrize("stage", ALL_STAGES)
def test_admin_update_accepts_every_engine_stage(stage: str) -> None:
    assert SafetyRuleUpdate(stage=stage).stage == stage


def test_egress_request_is_accepted_specifically() -> None:
    # The exact case from the field 422 (editing an egress.domain_rule rule).
    assert SafetyRuleUpdate(stage="egress_request").stage == "egress_request"
    assert (
        SafetyRuleCreate(stage="egress_request", check_id="egress.domain_rule").stage
        == "egress_request"
    )


def test_unknown_stage_is_still_rejected() -> None:
    with pytest.raises(ValueError):
        SafetyRuleUpdate(stage="not_a_real_stage")


def test_pattern_covers_exactly_the_engine_stage_set() -> None:
    # Drift guard: the alternation in the regex must equal the engine's
    # authoritative Stage values. If the engine adds a 7th stage, this fails
    # until the (derived) pattern picks it up — closing the gap that caused
    # the egress_request 422 in the first place.
    inner = _SAFETY_STAGE_PATTERN.removeprefix("^(").removesuffix(")$")
    assert set(inner.split("|")) == {s.value for s in Stage}
