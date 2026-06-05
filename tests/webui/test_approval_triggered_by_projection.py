"""Unit tests for the REST ``triggered_by`` projection helper.

``_triggered_by_to_response`` maps the ``approval_requests.triggered_by`` jsonb
onto the ``ApprovalTriggeredBy`` wire model. These call it directly (no DB) to
pin the "only a fully-formed safety_rule provenance projects" contract.
"""

from __future__ import annotations

from webui.schemas_v1 import ApprovalTriggeredBy
from webui.v1.approvals import _triggered_by_to_response


def test_well_formed_safety_rule_projects() -> None:
    out = _triggered_by_to_response(
        {
            "kind": "safety_rule",
            "rule_id": "rule-9",
            "check_id": "pii.regex",
            "stage": "pre_tool_call",
        }
    )
    assert isinstance(out, ApprovalTriggeredBy)
    assert out.kind == "safety_rule"
    assert out.rule_id == "rule-9"
    assert out.check_id == "pii.regex"
    assert out.stage == "pre_tool_call"


def test_none_and_business_policy_project_none() -> None:
    assert _triggered_by_to_response(None) is None
    # A business-policy approval has no provenance object at all.
    assert _triggered_by_to_response({}) is None


def test_unknown_kind_degrades_to_none() -> None:
    assert (
        _triggered_by_to_response(
            {
                "kind": "scheduled_task",  # V1 only emits safety_rule
                "rule_id": "r",
                "check_id": "c",
                "stage": "pre_tool_call",
            }
        )
        is None
    )


def test_missing_or_empty_fields_degrade_to_none() -> None:
    for bad in (
        {"kind": "safety_rule", "rule_id": "r", "check_id": "c"},  # no stage
        {"kind": "safety_rule", "rule_id": "", "check_id": "c", "stage": "s"},
        {"kind": "safety_rule", "rule_id": 9, "check_id": "c", "stage": "s"},
        "not-a-dict",
    ):
        assert _triggered_by_to_response(bad) is None  # type: ignore[arg-type]
