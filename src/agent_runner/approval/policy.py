"""Approval policy matching — pure functions.

SHARED between container-side ApprovalHookHandler and orchestrator-side
ApprovalEngine. Both import this exact file. The one-implementation
invariant eliminates parity risk.

Zero external dependencies: only stdlib types as input/output.

A "policy" here is a dict with at least the following keys (typed as
`object` because the shared surface intentionally avoids importing a
dataclass — the orchestrator loads rows from Postgres, the container
receives them over NATS, and both paths naturally produce dicts):

    id                 str | UUID
    enabled            bool
    mcp_server_name    str
    tool_name          str          # "*" matches any tool on the server
    condition_expr     dict         # see evaluate_condition
    priority           int          # higher wins
    updated_at         str | None   # ISO8601 timestamp; ties broken by newest
    auto_expire_minutes int | None  # used by select_strictest_policy

Match ordering: enabled + server + (tool == name or "*") + condition.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


_COMPARISON_OPS = frozenset({"==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains"})


def evaluate_condition(expr: dict[str, Any], params: dict[str, Any]) -> bool:
    """Evaluate a condition expression against tool parameters.

    Accepted forms:
      {"always": true}
      {"field": "amount", "op": ">", "value": 1000}
      {"and": [<expr>, ...]}
      {"or":  [<expr>, ...]}

    Ops: ==, !=, >, >=, <, <=, in, not_in, contains

    Semantics:
      - Missing field in params → condition does not match (returns False).
      - Type mismatch in comparison → returns False (no exception bubbles up).
      - Empty and/or lists are treated as non-matching (vacuous policies
        must not auto-match every call).
      - Unknown expression shapes → False (fail-closed at match time; the
        hook layer turns "no match" into "no block", which is safe for the
        unmatched policy alone; the caller is responsible for rejecting
        malformed policies at config time).
    """
    if not isinstance(expr, dict) or not expr:
        return False

    if expr.get("always") is True:
        return True

    if "and" in expr:
        sub = expr["and"]
        if not isinstance(sub, list) or not sub:
            return False
        return all(evaluate_condition(e, params) for e in sub if isinstance(e, dict))

    if "or" in expr:
        sub = expr["or"]
        if not isinstance(sub, list) or not sub:
            return False
        return any(evaluate_condition(e, params) for e in sub if isinstance(e, dict))

    if "field" in expr and "op" in expr:
        field = expr["field"]
        op = expr["op"]
        value = expr.get("value")
        if not isinstance(field, str) or op not in _COMPARISON_OPS:
            return False
        if field not in params:
            return False
        return _compare(params[field], op, value)

    return False


def _compare(left: Any, op: str, right: Any) -> bool:
    """Compare two values using the given operator.

    Returns False rather than raising on type errors, so a mis-typed
    value in a policy or tool param cannot produce an unhandled
    exception inside a PreToolUse hook.
    """
    try:
        if op == "==":
            return bool(left == right)
        if op == "!=":
            return bool(left != right)
        if op == ">":
            return bool(left > right)
        if op == ">=":
            return bool(left >= right)
        if op == "<":
            return bool(left < right)
        if op == "<=":
            return bool(left <= right)
        if op == "in":
            if not isinstance(right, (list, tuple, set, str)):
                return False
            return left in right
        if op == "not_in":
            if not isinstance(right, (list, tuple, set, str)):
                return False
            return left not in right
        if op == "contains":
            if not isinstance(left, (list, tuple, set, str, dict)):
                return False
            return right in left
    except TypeError:
        return False
    return False


# ---------------------------------------------------------------------------
# Policy selection
# ---------------------------------------------------------------------------


def _policy_sort_key(p: dict[str, Any]) -> tuple[int, str]:
    """Higher priority wins; ties broken by newest updated_at (str desc)."""
    priority = p.get("priority")
    if not isinstance(priority, int):
        priority = 0
    updated = p.get("updated_at") or ""
    if not isinstance(updated, str):
        updated = ""
    return (-priority, _neg_str(updated))


def _neg_str(s: str) -> str:
    """Sort strings in descending order by inverting codepoints.

    Used so that Python's stable ascending sort yields descending string
    order for updated_at ties without having to reverse the whole sort.
    """
    # A simple trick: map each character to its complement. Sort key only —
    # don't expose this to callers.
    return "".join(chr(0x10FFFF - ord(c)) for c in s)


def find_matching_policy(
    policies: list[dict[str, Any]],
    mcp_server_name: str,
    tool_name: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Find the highest-priority enabled policy matching this action.

    Match requires all of:
      - policy["enabled"] is True
      - policy["mcp_server_name"] == mcp_server_name
      - policy["tool_name"] == "*" OR == tool_name
      - evaluate_condition(policy["condition_expr"], params)

    Multiple matches: highest priority wins. Ties: most recent updated_at.
    """
    matches: list[dict[str, Any]] = []
    for p in policies:
        if not p.get("enabled"):
            continue
        if p.get("mcp_server_name") != mcp_server_name:
            continue
        p_tool = p.get("tool_name")
        if p_tool != "*" and p_tool != tool_name:
            continue
        cond = p.get("condition_expr")
        if not isinstance(cond, dict):
            continue
        if not evaluate_condition(cond, params):
            continue
        matches.append(p)

    if not matches:
        return None

    matches.sort(key=_policy_sort_key)
    return matches[0]


def find_matching_policies_for_actions(
    policies: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> list[dict[str, Any] | None]:
    """For batch proposals: return the matched policy (or None) per action.

    Each action is expected to have keys ``mcp_server``, ``tool_name``,
    ``params``. The output list has the same length as ``actions`` and
    preserves order.
    """
    result: list[dict[str, Any] | None] = []
    for action in actions:
        server = action.get("mcp_server") or action.get("mcp_server_name") or ""
        tool = action.get("tool_name") or ""
        params = action.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        result.append(find_matching_policy(policies, str(server), str(tool), params))
    return result


def select_strictest_policy(matched: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the strictest policy among multiple matches for a batch request.

    Strictness ranking:
      1. Highest priority.
      2. Shortest non-null auto_expire_minutes (tighter deadline = stricter).
      3. First policy in list order (stable tiebreaker).

    The caller must pass a non-empty list; an empty list is a programmer
    error at this level, since "no match" is represented by the caller
    not calling into this function at all.
    """
    if not matched:
        raise ValueError("select_strictest_policy requires a non-empty list")

    def _key(p: dict[str, Any]) -> tuple[int, int]:
        priority = p.get("priority")
        if not isinstance(priority, int):
            priority = 0
        expire = p.get("auto_expire_minutes")
        # Absent or non-positive expiry = no deadline; treat as least strict.
        expire_rank = expire if isinstance(expire, int) and expire > 0 else 10**9
        return (-priority, expire_rank)

    best_key = _key(matched[0])
    best = matched[0]
    for p in matched[1:]:
        k = _key(p)
        if k < best_key:
            best_key = k
            best = p
    return best


# ---------------------------------------------------------------------------
# Action hashing (idempotency + dedup key)
# ---------------------------------------------------------------------------


def compute_action_hash(tool_name: str, params: dict[str, Any]) -> str:
    """Deterministic SHA-256 of the canonical (tool, params) pair.

    Canonical form:
        {"tool": tool_name, "params": params}
    serialised with ``json.dumps(..., sort_keys=True, separators=(",",":"))``.

    Used as both:
      - the MCP idempotency key sent in the X-Idempotency-Key header
        when the orchestrator executes an approved action, and
      - the dedup key for auto-intercept requests, so the same action
        submitted twice within the 5-minute window does not create two
        pending approvals.

    Determinism must survive key ordering — tests assert that.

    ``params`` is expected to be JSON-serializable. Non-serializable
    values (e.g. ``set``, user-defined objects) fall through to
    ``_json_default`` which calls ``str(obj)``. That is deterministic
    within a single Python run but *may* differ across Python versions
    for types whose ``__str__`` includes implementation details; callers
    that need long-term stability across upgrades should normalize
    their params upstream.
    """
    canonical = json.dumps(
        {"tool": tool_name, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(obj: Any) -> Any:
    """Last-resort serializer for values that json.dumps cannot handle.

    We don't want compute_action_hash to raise mid-hook; falling back to
    str(obj) is deterministic for the same input and good enough as an
    idempotency key.
    """
    return str(obj)
