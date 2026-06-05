"""P0-2: the shared rule validator + the validate⇔create invariant.

These exercise :mod:`webui.safety_validation` directly (no DB) using
cheap checks, so the slow-check budget branch — the only DB-touching
path — is never hit. The endpoint-level dry-run + warnings are covered
in ``tests/webui/test_v1_safety.py`` against a real Postgres.

The invariant test is the load-bearing one: a body that
``collect_rule_errors`` rejects MUST also make the admin write path's
``_validate_safety_rule_body`` raise, and vice-versa. Both call the same
collector, so this can never regress silently — but pinning it stops a
future refactor from re-forking the two paths.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from webui.admin import _validate_safety_rule_body
from webui.safety_validation import collect_rule_errors, compute_info

TENANT = "00000000-0000-0000-0000-000000000001"


async def _errs(
    check_id: str, stage: str, config: dict
) -> list[dict]:
    return await collect_rule_errors(
        check_id, stage, config, tenant_id=TENANT, coworker_id=None
    )


@pytest.mark.asyncio
async def test_valid_body_has_no_errors() -> None:
    assert await _errs("pii.regex", "pre_tool_call", {"patterns": {"SSN": True}}) == []


@pytest.mark.asyncio
async def test_unknown_check_id_short_circuits() -> None:
    errs = await _errs("does.not.exist", "pre_tool_call", {})
    assert len(errs) == 1
    assert errs[0]["type"] == "unknown_check_id"
    assert errs[0]["loc"] == ["body", "check_id"]


@pytest.mark.asyncio
async def test_stage_not_supported() -> None:
    errs = await _errs("pii.regex", "egress_request", {"patterns": {"SSN": True}})
    assert any(e["type"] == "stage_not_supported" for e in errs)


@pytest.mark.asyncio
async def test_extra_config_key_is_field_scoped() -> None:
    errs = await _errs(
        "pii.regex", "pre_tool_call", {"patterns": {"SSN": True}, "nope": 1}
    )
    assert any(
        e["type"] == "extra_forbidden" and e["loc"] == ["body", "config", "nope"]
        for e in errs
    )


@pytest.mark.asyncio
async def test_unknown_pattern_key_rejected_via_enum() -> None:
    errs = await _errs("pii.regex", "pre_tool_call", {"patterns": {"SNN": True}})
    assert any(e["type"] == "enum" for e in errs)


@pytest.mark.asyncio
async def test_bad_action_override() -> None:
    errs = await _errs(
        "pii.regex",
        "pre_tool_call",
        {"patterns": {"SSN": True}, "action_override": "teleport"},
    )
    assert any(
        e["type"] == "invalid_action_override"
        and e["loc"] == ["body", "config", "action_override"]
        for e in errs
    )


@pytest.mark.asyncio
async def test_multiple_config_errors_surface_together() -> None:
    # The P0-2 invalid-case example: wrong key + missing required key
    # both reported in one pass so the SPA can map each to a field.
    errs = await _errs("domain_allowlist", "pre_tool_call", {"hosts": ["x"]})
    types = {e["type"] for e in errs}
    assert "extra_forbidden" in types
    assert "missing" in types
    missing = next(e for e in errs if e["type"] == "missing")
    assert missing["loc"] == ["body", "config", "allowed_hosts"]


# --- compute_info -----------------------------------------------------


def test_info_resolves_natural_action() -> None:
    info = compute_info("pii.regex", "pre_tool_call", {})
    assert info is not None
    assert info["action_resolution"] == "block"
    assert set(info["stage_supported_actions"]) == {
        "allow",
        "block",
        "warn",
        "require_approval",
    }


def test_info_override_wins_over_natural() -> None:
    info = compute_info("pii.regex", "pre_tool_call", {"action_override": "warn"})
    assert info is not None
    assert info["action_resolution"] == "warn"


def test_info_none_for_unknown_check() -> None:
    assert compute_info("does.not.exist", "pre_tool_call", {}) is None


# --- the invariant ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("check_id", "stage", "config"),
    [
        ("pii.regex", "pre_tool_call", {"patterns": {"SSN": True}}),  # valid
        ("does.not.exist", "pre_tool_call", {}),  # unknown check
        ("pii.regex", "egress_request", {"patterns": {"SSN": True}}),  # stage
        ("pii.regex", "pre_tool_call", {"patterns": {"SNN": True}}),  # bad key
        ("pii.regex", "pre_tool_call", {"patterns": {"SSN": "yes"}}),  # bad bool
        ("pii.regex", "pre_tool_call", {"nope": 1}),  # extra key
        ("domain_allowlist", "pre_tool_call", {"allowed_hosts": ["a"]}),  # valid
        ("domain_allowlist", "pre_tool_call", {"allowed_hosts": []}),  # min_len
        (
            "pii.regex",
            "pre_tool_call",
            {"patterns": {"SSN": True}, "action_override": "teleport"},
        ),
    ],
)
async def test_collect_agrees_with_admin_write_path(
    check_id: str, stage: str, config: dict
) -> None:
    errs = await collect_rule_errors(
        check_id, stage, config, tenant_id=TENANT, coworker_id=None
    )
    raised = False
    try:
        await _validate_safety_rule_body(
            check_id, stage, config, tenant_id=TENANT, coworker_id=None
        )
    except HTTPException as exc:
        raised = True
        assert exc.status_code == 400
    assert raised == bool(errs), (
        f"validate/create disagree on {check_id}/{stage}/{config}: "
        f"errors={errs} raised={raised}"
    )
