"""Unit tests for approval policy matching.

Focus on adversarial cases: boundaries, missing fields, type mismatches,
priority tiebreaks, key-order invariance of action hashes. A mutation to
the source (flipping < to <=, swapping any/all, dropping the wildcard
branch, reversing the priority direction) should break at least one
test here.
"""

from __future__ import annotations

import pytest

from agent_runner.approval.policy import (
    compute_action_hash,
    evaluate_condition,
    find_matching_policies_for_actions,
    find_matching_policy,
    select_strictest_policy,
)

# ---------------------------------------------------------------------------
# evaluate_condition: sentinel forms
# ---------------------------------------------------------------------------


class TestEvaluateConditionSentinels:
    def test_always_true_matches_everything(self) -> None:
        assert evaluate_condition({"always": True}, {}) is True
        assert evaluate_condition({"always": True}, {"amount": 1}) is True

    def test_always_false_sentinel_is_not_always_true(self) -> None:
        # Only "always": True is magical. "always": False falls through and
        # ends up as an unknown shape -> False.
        assert evaluate_condition({"always": False}, {}) is False

    def test_empty_dict_never_matches(self) -> None:
        assert evaluate_condition({}, {"amount": 100}) is False

    def test_non_dict_expression_never_matches(self) -> None:
        # Defensive: if serialization produced a list or None, don't crash.
        assert evaluate_condition(None, {}) is False  # type: ignore[arg-type]
        assert evaluate_condition([], {}) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# evaluate_condition: comparison ops
# ---------------------------------------------------------------------------


class TestEvaluateConditionNumeric:
    def test_gt_strict_not_equal(self) -> None:
        # The strict inequality must exclude the boundary.
        assert evaluate_condition({"field": "x", "op": ">", "value": 100}, {"x": 100}) is False
        assert evaluate_condition({"field": "x", "op": ">", "value": 100}, {"x": 101}) is True

    def test_gte_includes_boundary(self) -> None:
        assert evaluate_condition({"field": "x", "op": ">=", "value": 100}, {"x": 100}) is True
        assert evaluate_condition({"field": "x", "op": ">=", "value": 100}, {"x": 99}) is False

    def test_lt_and_lte_symmetric(self) -> None:
        assert evaluate_condition({"field": "x", "op": "<", "value": 100}, {"x": 100}) is False
        assert evaluate_condition({"field": "x", "op": "<", "value": 100}, {"x": 99}) is True
        assert evaluate_condition({"field": "x", "op": "<=", "value": 100}, {"x": 100}) is True

    def test_negative_values(self) -> None:
        assert evaluate_condition({"field": "x", "op": ">", "value": -10}, {"x": -5}) is True
        assert evaluate_condition({"field": "x", "op": "<", "value": 0}, {"x": -1}) is True


class TestEvaluateConditionEquality:
    def test_eq_ne(self) -> None:
        assert evaluate_condition({"field": "a", "op": "==", "value": "x"}, {"a": "x"}) is True
        assert evaluate_condition({"field": "a", "op": "!=", "value": "x"}, {"a": "y"}) is True
        assert evaluate_condition({"field": "a", "op": "==", "value": "x"}, {"a": "y"}) is False

    def test_eq_exact_type(self) -> None:
        # Python considers 1 == True to be True; we don't try to rescue
        # callers from that — just document it.
        assert evaluate_condition({"field": "a", "op": "==", "value": 1}, {"a": True}) is True


class TestEvaluateConditionMembership:
    def test_in_with_list(self) -> None:
        assert evaluate_condition(
            {"field": "env", "op": "in", "value": ["prod", "staging"]},
            {"env": "prod"},
        ) is True
        assert evaluate_condition(
            {"field": "env", "op": "in", "value": ["prod", "staging"]},
            {"env": "dev"},
        ) is False

    def test_in_rejects_non_iterable_value(self) -> None:
        # Mis-typed policy: value is a number. Must not raise.
        assert evaluate_condition(
            {"field": "env", "op": "in", "value": 123},
            {"env": "prod"},
        ) is False

    def test_not_in_is_complement_when_field_present(self) -> None:
        assert evaluate_condition(
            {"field": "env", "op": "not_in", "value": ["prod"]},
            {"env": "dev"},
        ) is True
        assert evaluate_condition(
            {"field": "env", "op": "not_in", "value": ["prod"]},
            {"env": "prod"},
        ) is False

    def test_contains_on_list(self) -> None:
        assert evaluate_condition(
            {"field": "tags", "op": "contains", "value": "high-risk"},
            {"tags": ["low", "high-risk"]},
        ) is True
        assert evaluate_condition(
            {"field": "tags", "op": "contains", "value": "missing"},
            {"tags": ["low"]},
        ) is False

    def test_contains_on_string(self) -> None:
        # Substring match — useful for scanning memo/notes fields.
        assert evaluate_condition(
            {"field": "note", "op": "contains", "value": "refund"},
            {"note": "emergency refund"},
        ) is True


# ---------------------------------------------------------------------------
# evaluate_condition: failure modes that must NOT raise
# ---------------------------------------------------------------------------


class TestEvaluateConditionMissingField:
    def test_missing_field_returns_false_not_raises(self) -> None:
        # This is the most subtle contract. If the tool called omits a
        # field the policy cares about, we must treat it as non-matching
        # instead of blowing up the hook chain.
        assert evaluate_condition(
            {"field": "amount", "op": ">", "value": 100},
            {},
        ) is False
        assert evaluate_condition(
            {"field": "amount", "op": "==", "value": None},
            {"other": 1},
        ) is False


class TestEvaluateConditionTypeMismatch:
    def test_string_vs_number_comparison_returns_false(self) -> None:
        # Python 3 raises TypeError for "100" > 50. The policy module
        # must catch this and return False.
        assert evaluate_condition(
            {"field": "amount", "op": ">", "value": 50},
            {"amount": "100"},
        ) is False

    def test_unknown_operator_returns_false(self) -> None:
        assert evaluate_condition(
            {"field": "amount", "op": "~=", "value": 1},
            {"amount": 1},
        ) is False


# ---------------------------------------------------------------------------
# evaluate_condition: boolean connectives
# ---------------------------------------------------------------------------


class TestEvaluateConditionConnectives:
    def test_and_requires_all_branches_to_match(self) -> None:
        expr = {
            "and": [
                {"field": "amount", "op": ">", "value": 100},
                {"field": "currency", "op": "==", "value": "USD"},
            ]
        }
        assert evaluate_condition(expr, {"amount": 150, "currency": "USD"}) is True
        assert evaluate_condition(expr, {"amount": 50, "currency": "USD"}) is False
        assert evaluate_condition(expr, {"amount": 150, "currency": "EUR"}) is False

    def test_or_requires_any_branch_to_match(self) -> None:
        expr = {
            "or": [
                {"field": "amount", "op": ">", "value": 1000},
                {"field": "priority", "op": "==", "value": "critical"},
            ]
        }
        assert evaluate_condition(expr, {"amount": 50, "priority": "critical"}) is True
        assert evaluate_condition(expr, {"amount": 2000, "priority": "low"}) is True
        assert evaluate_condition(expr, {"amount": 50, "priority": "low"}) is False

    def test_empty_connective_lists_do_not_match(self) -> None:
        # Vacuous all([]) == True, but a policy with empty `and` is almost
        # certainly a config bug; treating it as "matches everything" would
        # be a footgun, so we define it as "does not match".
        assert evaluate_condition({"and": []}, {}) is False
        assert evaluate_condition({"or": []}, {}) is False

    def test_nested_connectives(self) -> None:
        expr = {
            "or": [
                {"field": "amount", "op": ">", "value": 10000},
                {
                    "and": [
                        {"field": "amount", "op": ">", "value": 1000},
                        {"field": "vip", "op": "==", "value": True},
                    ]
                },
            ]
        }
        assert evaluate_condition(expr, {"amount": 50000, "vip": False}) is True
        assert evaluate_condition(expr, {"amount": 1500, "vip": True}) is True
        assert evaluate_condition(expr, {"amount": 1500, "vip": False}) is False


# ---------------------------------------------------------------------------
# find_matching_policy
# ---------------------------------------------------------------------------


def _policy(
    *,
    id: str = "p",
    server: str = "erp",
    tool: str = "refund",
    cond: dict[str, object] | None = None,
    priority: int = 0,
    enabled: bool = True,
    updated_at: str = "2026-04-01T00:00:00+00:00",
    auto_expire_minutes: int | None = 60,
) -> dict[str, object]:
    return {
        "id": id,
        "enabled": enabled,
        "mcp_server_name": server,
        "tool_name": tool,
        "condition_expr": cond or {"always": True},
        "priority": priority,
        "updated_at": updated_at,
        "auto_expire_minutes": auto_expire_minutes,
    }


class TestFindMatchingPolicy:
    def test_returns_none_when_no_policies(self) -> None:
        assert find_matching_policy([], "erp", "refund", {}) is None

    def test_disabled_policy_is_skipped(self) -> None:
        p = _policy(enabled=False)
        assert find_matching_policy([p], "erp", "refund", {"amount": 10}) is None

    def test_server_mismatch_never_matches(self) -> None:
        p = _policy(server="erp")
        assert find_matching_policy([p], "crm", "refund", {}) is None

    def test_tool_wildcard_matches_any_tool_on_same_server(self) -> None:
        p = _policy(tool="*")
        assert find_matching_policy([p], "erp", "cancel_order", {})["id"] == "p"  # type: ignore[index]

    def test_tool_name_exact_match(self) -> None:
        p = _policy(tool="refund")
        assert find_matching_policy([p], "erp", "refund", {}) is not None
        assert find_matching_policy([p], "erp", "cancel_order", {}) is None

    def test_higher_priority_wins(self) -> None:
        low = _policy(id="low", priority=0)
        high = _policy(id="high", priority=10)
        result = find_matching_policy([low, high], "erp", "refund", {})
        assert result is not None and result["id"] == "high"

    def test_priority_tie_prefers_newer_updated_at(self) -> None:
        older = _policy(id="older", priority=5, updated_at="2026-03-01T00:00:00+00:00")
        newer = _policy(id="newer", priority=5, updated_at="2026-04-01T00:00:00+00:00")
        result = find_matching_policy([older, newer], "erp", "refund", {})
        assert result is not None and result["id"] == "newer"

    def test_policy_with_non_dict_condition_is_ignored(self) -> None:
        bad = _policy(cond={})  # empty dict -> never matches
        bad["condition_expr"] = None  # type: ignore[assignment]
        assert find_matching_policy([bad], "erp", "refund", {}) is None

    def test_wildcard_tool_with_condition(self) -> None:
        p = _policy(
            tool="*",
            cond={"field": "amount", "op": ">", "value": 1000},
        )
        assert find_matching_policy([p], "erp", "anything", {"amount": 2000}) is not None
        assert find_matching_policy([p], "erp", "anything", {"amount": 500}) is None


# ---------------------------------------------------------------------------
# find_matching_policies_for_actions
# ---------------------------------------------------------------------------


class TestFindMatchingPoliciesForActions:
    def test_empty_actions_returns_empty(self) -> None:
        assert find_matching_policies_for_actions([_policy()], []) == []

    def test_per_action_match_preserves_order(self) -> None:
        p = _policy(tool="refund", cond={"field": "amount", "op": ">", "value": 100})
        actions = [
            {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 50}},
            {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 500}},
            {"mcp_server": "crm", "tool_name": "refund", "params": {"amount": 500}},
        ]
        results = find_matching_policies_for_actions([p], actions)
        assert len(results) == 3
        assert results[0] is None
        assert results[1] is not None
        assert results[2] is None  # server mismatch

    def test_malformed_action_does_not_raise(self) -> None:
        # Agent might hand back a garbage params field; treat as empty dict.
        results = find_matching_policies_for_actions(
            [_policy(cond={"always": True})],
            [{"mcp_server": "erp", "tool_name": "refund", "params": None}],
        )
        assert results[0] is not None


# ---------------------------------------------------------------------------
# select_strictest_policy
# ---------------------------------------------------------------------------


class TestSelectStrictestPolicy:
    def test_raises_on_empty(self) -> None:
        with pytest.raises(ValueError):
            select_strictest_policy([])

    def test_highest_priority_wins(self) -> None:
        a = _policy(id="a", priority=1, auto_expire_minutes=5)
        b = _policy(id="b", priority=10, auto_expire_minutes=60)
        assert select_strictest_policy([a, b])["id"] == "b"

    def test_priority_tie_prefers_shorter_expiry(self) -> None:
        a = _policy(id="a", priority=5, auto_expire_minutes=60)
        b = _policy(id="b", priority=5, auto_expire_minutes=5)
        assert select_strictest_policy([a, b])["id"] == "b"

    def test_missing_expiry_treated_as_loose(self) -> None:
        a = _policy(id="a", priority=5, auto_expire_minutes=60)
        b = _policy(id="b", priority=5, auto_expire_minutes=None)
        # "None" expire = loosest, so policy with real deadline wins.
        assert select_strictest_policy([a, b])["id"] == "a"


# ---------------------------------------------------------------------------
# compute_action_hash
# ---------------------------------------------------------------------------


class TestComputeActionHash:
    def test_deterministic_across_key_orders(self) -> None:
        a = compute_action_hash("refund", {"amount": 100, "currency": "USD"})
        b = compute_action_hash("refund", {"currency": "USD", "amount": 100})
        assert a == b

    def test_deterministic_across_nested_key_orders(self) -> None:
        a = compute_action_hash(
            "refund",
            {"amount": 100, "meta": {"x": 1, "y": 2}},
        )
        b = compute_action_hash(
            "refund",
            {"meta": {"y": 2, "x": 1}, "amount": 100},
        )
        assert a == b

    def test_different_tool_names_hash_differently(self) -> None:
        params = {"amount": 100}
        assert compute_action_hash("refund", params) != compute_action_hash("charge", params)

    def test_different_params_hash_differently(self) -> None:
        assert compute_action_hash("refund", {"amount": 100}) != compute_action_hash(
            "refund", {"amount": 101}
        )

    def test_empty_params_is_distinct_from_missing(self) -> None:
        # {"amount": None} and {} must NOT collide, because an explicit
        # None might trigger a different server-side effect.
        h1 = compute_action_hash("refund", {})
        h2 = compute_action_hash("refund", {"amount": None})
        assert h1 != h2

    def test_non_json_serializable_falls_back_to_str(self) -> None:
        # sets aren't JSON-serializable; the module should not raise.
        # This is a property test: run it twice, must return same value.
        class Custom:
            def __str__(self) -> str:
                return "custom-value"

        a = compute_action_hash("refund", {"obj": Custom()})
        b = compute_action_hash("refund", {"obj": Custom()})
        assert a == b

    def test_hash_is_hex_sha256_length(self) -> None:
        h = compute_action_hash("refund", {"amount": 1})
        assert len(h) == 64
        int(h, 16)  # must be valid hex
