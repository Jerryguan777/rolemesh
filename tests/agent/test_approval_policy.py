"""Adversarial tests for the pure HITL policy matcher.

These target the two things that make this gate dangerous if wrong:

1. **Fail-closed** — the contract (docs/21-hitl-approval-plan.md §7) says a
   gate that cannot evaluate must require approval. Every malformed /
   ambiguous input below must therefore return ``True`` (approval required).
   A regression that lets one of these slip to ``False`` silently lets a
   gated tool call through without a human — the exact failure this feature
   exists to prevent.
2. **Tiebreak determinism** — when several policies match, the wrong one
   winning means the wrong condition gated the call.

We deliberately ship real inputs + real expectations, not a re-derivation
of the matcher's branches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent_runner.approval.policy import (
    ApprovalPolicy,
    evaluate_condition,
    evaluate_condition_explained,
    find_matching_policy,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _policy(
    *,
    pid: str = "p",
    server: str = "stripe",
    tool: str = "create_charge",
    expr: dict | None = None,
    enabled: bool = True,
    priority: int = 0,
    updated_at: datetime | None = None,
) -> ApprovalPolicy:
    return ApprovalPolicy(
        id=pid,
        tenant_id="t",
        mcp_server_name=server,
        tool_name=tool,
        condition_expr=expr if expr is not None else {"always": True},
        enabled=enabled,
        priority=priority,
        updated_at=updated_at or _T0,
    )


# ---------------------------------------------------------------------------
# evaluate_condition — the matching (True) path
# ---------------------------------------------------------------------------


def test_always_true_matches() -> None:
    assert evaluate_condition({"always": True}, {}) is True


def test_always_false_is_the_only_clean_no_match() -> None:
    # {"always": false} is the one explicitly-false leaf. Must NOT be coerced
    # to fail-closed True, or an "always allow" policy could never exist.
    assert evaluate_condition({"always": False}, {"anything": 1}) is False


@pytest.mark.parametrize(
    ("op", "value", "param", "expected"),
    [
        (">", 100, 200, True),
        (">", 100, 100, False),    # boundary: not strictly greater
        (">", 100, 50, False),
        (">=", 100, 100, True),    # boundary: equal counts
        ("<", 100, 50, True),
        ("<=", 100, 100, True),
        ("==", "prod", "prod", True),
        ("==", "prod", "dev", False),
        ("!=", "prod", "dev", True),
        ("!=", "prod", "prod", False),
    ],
)
def test_scalar_operators_at_boundaries(
    op: str, value: object, param: object, expected: bool
) -> None:
    assert evaluate_condition({"field": "x", "op": op, "value": value}, {"x": param}) is expected


def test_in_and_not_in_membership() -> None:
    assert evaluate_condition({"field": "env", "op": "in", "value": ["prod", "stage"]}, {"env": "prod"}) is True
    assert evaluate_condition({"field": "env", "op": "in", "value": ["prod"]}, {"env": "dev"}) is False
    assert evaluate_condition({"field": "env", "op": "not_in", "value": ["prod"]}, {"env": "dev"}) is True
    assert evaluate_condition({"field": "env", "op": "not_in", "value": ["dev"]}, {"env": "dev"}) is False


def test_contains_on_list_and_string() -> None:
    assert evaluate_condition({"field": "tags", "op": "contains", "value": "vip"}, {"tags": ["vip", "x"]}) is True
    assert evaluate_condition({"field": "tags", "op": "contains", "value": "vip"}, {"tags": ["x"]}) is False
    assert evaluate_condition({"field": "name", "op": "contains", "value": "ad"}, {"name": "admin"}) is True


def test_present_null_field_is_a_real_value_not_missing() -> None:
    # A field that exists but is None must be compared, not treated as the
    # "missing field" fail-closed case. None == None is a clean True/False.
    assert evaluate_condition({"field": "x", "op": "==", "value": None}, {"x": None}) is True
    assert evaluate_condition({"field": "x", "op": "!=", "value": None}, {"x": None}) is False


# ---------------------------------------------------------------------------
# evaluate_condition — FAIL-CLOSED: every one of these MUST return True
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "params", "why"),
    [
        ({"field": "amount", "op": ">", "value": 100}, {}, "missing field"),
        ({"field": "amount", "op": ">", "value": 100}, {"amount": "abc"}, "str > int type mismatch"),
        ({"field": "amount", "op": ">", "value": 100}, {"amount": None}, "None > int type mismatch"),
        ({"field": "x", "op": "in", "value": "prod"}, {"x": "p"}, "'in' against a bare string is not membership"),
        ({"field": "x", "op": "in", "value": 5}, {"x": 5}, "'in' against a non-iterable"),
        ({"field": "x", "op": "contains", "value": "a"}, {"x": 123}, "contains on a non-container"),
        ({"field": "x", "op": "nonsense", "value": 1}, {"x": 1}, "unknown operator"),
        ({"op": ">", "value": 1}, {"x": 1}, "leaf missing 'field'"),
        ({"field": "x", "value": 1}, {"x": 1}, "leaf missing 'op'"),
        ({"field": "x", "op": ">"}, {"x": 1}, "leaf missing 'value'"),
        ({"always": "yes"}, {}, "'always' not a bool"),
        ({"or": []}, {}, "empty 'or' is malformed, not vacuously false"),
        ({"and": []}, {}, "empty 'and' is malformed"),
        ({"or": "notalist"}, {}, "'or' not a list"),
        ({}, {}, "empty expr"),
        ("notadict", {}, "expr not a dict"),
        (None, {}, "expr is None"),
        (42, {}, "expr is an int"),
    ],
)
def test_unevaluable_conditions_fail_closed(expr: object, params: dict, why: str) -> None:
    assert evaluate_condition(expr, params) is True, f"should fail closed: {why}"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# evaluate_condition_explained — the reason channel (turns a silent gate into
# a fixable signal). Invariant: reason is non-None EXACTLY when the True came
# from fail-closed (couldn't evaluate), never from a genuine match.
# ---------------------------------------------------------------------------


def test_explained_clean_true_carries_no_reason() -> None:
    assert evaluate_condition_explained({"always": True}, {}) == (True, None)
    assert evaluate_condition_explained(
        {"field": "x", "op": "==", "value": 1}, {"x": 1}
    ) == (True, None)


def test_explained_clean_false_carries_no_reason() -> None:
    assert evaluate_condition_explained(
        {"field": "x", "op": "==", "value": 1}, {"x": 2}
    ) == (False, None)


def test_explained_missing_field_gates_and_names_the_field() -> None:
    # The real-world bug: a field typo ('paht' for 'path') makes the condition
    # un-evaluable, so it fail-closes and gates every call. The reason must name
    # the offending field so the user can fix the typo.
    matched, reason = evaluate_condition_explained(
        {"field": "paht", "op": "==", "value": "x.txt"}, {"path": "x.txt"}
    )
    assert matched is True
    assert reason is not None
    assert "paht" in reason


def test_explained_missing_branch_in_and_still_reports_a_reason() -> None:
    # First branch true (so AND evaluation reaches the second), second branch
    # references a missing field: the whole AND fail-closes True and the reason
    # points at the un-evaluable branch.
    expr = {
        "and": [
            {"field": "amount", "op": ">", "value": 100},
            {"field": "currancy", "op": "==", "value": "USD"},
        ],
    }
    matched, reason = evaluate_condition_explained(expr, {"amount": 500})
    assert matched is True
    assert reason is not None and "currancy" in reason


def test_explained_malformed_leaf_gates_with_a_generic_reason() -> None:
    matched, reason = evaluate_condition_explained({"op": ">", "value": 1}, {"x": 1})
    assert matched is True
    assert reason is not None  # generic ("malformed") is fine here


def test_empty_or_does_not_short_circuit_to_false() -> None:
    # Regression guard: a naive any([]) implementation returns False, which
    # would silently DROP the gate. This is the single most dangerous bug in
    # the matcher, so it gets its own named test.
    assert evaluate_condition({"or": []}, {"amount": 999}) is True


# ---------------------------------------------------------------------------
# evaluate_condition — nested boolean composition
# ---------------------------------------------------------------------------


def test_nested_and_all_true() -> None:
    expr = {"and": [
        {"field": "amount", "op": ">", "value": 100},
        {"field": "currency", "op": "==", "value": "USD"},
    ]}
    assert evaluate_condition(expr, {"amount": 500, "currency": "USD"}) is True


def test_nested_and_one_clean_false_is_no_match() -> None:
    expr = {"and": [
        {"field": "amount", "op": ">", "value": 100},
        {"field": "currency", "op": "==", "value": "USD"},
    ]}
    assert evaluate_condition(expr, {"amount": 500, "currency": "EUR"}) is False


def test_nested_and_with_one_unevaluable_branch_fails_closed() -> None:
    # amount cleanly fails (50 not > 100) but currency field is missing.
    # The missing branch raises -> whole expr fails closed True, even though
    # one branch was a clean False. A gate that can't fully evaluate asks.
    expr = {"or": [
        {"field": "amount", "op": ">", "value": 100},
        {"field": "currency", "op": "==", "value": "USD"},
    ]}
    assert evaluate_condition(expr, {"amount": 50}) is True


def test_or_with_one_true_branch_matches() -> None:
    expr = {"or": [
        {"field": "amount", "op": ">", "value": 100},
        {"field": "env", "op": "==", "value": "prod"},
    ]}
    assert evaluate_condition(expr, {"amount": 5, "env": "prod"}) is True


def test_deeply_nested_composition() -> None:
    expr = {"and": [
        {"or": [
            {"field": "amount", "op": ">", "value": 1000},
            {"field": "vip", "op": "==", "value": True},
        ]},
        {"field": "currency", "op": "in", "value": ["USD", "EUR"]},
    ]}
    assert evaluate_condition(expr, {"amount": 5, "vip": True, "currency": "EUR"}) is True
    assert evaluate_condition(expr, {"amount": 5, "vip": False, "currency": "EUR"}) is False


# ---------------------------------------------------------------------------
# find_matching_policy
# ---------------------------------------------------------------------------


def test_no_policies_returns_none() -> None:
    assert find_matching_policy([], mcp_server_name="stripe", tool_name="t", params={}) is None


def test_server_must_match() -> None:
    pols = [_policy(server="stripe")]
    assert find_matching_policy(pols, mcp_server_name="github", tool_name="create_charge", params={}) is None


def test_wildcard_tool_matches_any_tool_on_server() -> None:
    pols = [_policy(tool="*")]
    got = find_matching_policy(pols, mcp_server_name="stripe", tool_name="anything", params={})
    assert got is not None


def test_exact_tool_does_not_match_other_tool() -> None:
    pols = [_policy(tool="create_charge")]
    assert find_matching_policy(pols, mcp_server_name="stripe", tool_name="refund", params={}) is None


def test_disabled_policy_never_matches() -> None:
    # Even with an always-true condition, a disabled policy is invisible.
    pols = [_policy(enabled=False, expr={"always": True})]
    assert find_matching_policy(pols, mcp_server_name="stripe", tool_name="create_charge", params={}) is None


def test_condition_false_excludes_policy() -> None:
    pols = [_policy(expr={"field": "amount", "op": ">", "value": 100})]
    assert find_matching_policy(pols, mcp_server_name="stripe", tool_name="create_charge", params={"amount": 5}) is None


def test_highest_priority_wins() -> None:
    pols = [
        _policy(pid="low", priority=1),
        _policy(pid="high", priority=10),
        _policy(pid="mid", priority=5),
    ]
    got = find_matching_policy(pols, mcp_server_name="stripe", tool_name="create_charge", params={})
    assert got is not None and got.id == "high"


def test_priority_tie_breaks_to_newest_updated_at() -> None:
    pols = [
        _policy(pid="old", priority=5, updated_at=_T0),
        _policy(pid="new", priority=5, updated_at=_T0 + timedelta(days=1)),
    ]
    got = find_matching_policy(pols, mcp_server_name="stripe", tool_name="create_charge", params={})
    assert got is not None and got.id == "new", "tie must break to newest updated_at"


def test_priority_beats_recency() -> None:
    # A newer-but-lower-priority policy must NOT win over an older higher one.
    pols = [
        _policy(pid="newer_low", priority=1, updated_at=_T0 + timedelta(days=10)),
        _policy(pid="older_high", priority=9, updated_at=_T0),
    ]
    got = find_matching_policy(pols, mcp_server_name="stripe", tool_name="create_charge", params={})
    assert got is not None and got.id == "older_high"


def test_matcher_uses_fail_closed_condition() -> None:
    # A policy whose condition references a field the call doesn't carry must
    # still match (fail-closed) — the matcher and the leaf evaluator share the
    # same conservative bias.
    pols = [_policy(expr={"field": "amount", "op": ">", "value": 100})]
    got = find_matching_policy(pols, mcp_server_name="stripe", tool_name="create_charge", params={})
    assert got is not None
