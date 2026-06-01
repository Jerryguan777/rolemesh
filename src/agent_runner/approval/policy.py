"""Pure, fail-closed policy matching for HITL tool approval.

Zero external dependencies (stdlib only) — imported by both the
container approval hook and the orchestrator (docs/21-hitl-approval-plan.md
§7). Two entry points:

* ``evaluate_condition(expr, params)`` — does this structured condition
  match the tool-call arguments? **Fail-closed**: a missing field, a type
  mismatch, a malformed expression, or any exception returns ``True``
  (treat as a match ⇒ require human approval). A gate that cannot evaluate
  itself must err toward asking the human.
* ``find_matching_policy(policies, ...)`` — of the enabled policies for a
  tenant, which one (if any) governs this ``(mcp_server, tool, params)``
  call. Highest ``priority`` wins; ties break to the newest ``updated_at``.

The condition language is structured comparison only — no expression
language, no ``eval`` (§2 scope redline)::

    {"always": true}
    {"field": "amount", "op": ">", "value": 100}
    {"and": [ <expr>, ... ]}
    {"or":  [ <expr>, ... ]}

Operators: ``== != > >= < <= in not_in contains``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

__all__ = [
    "ApprovalPolicy",
    "ConditionValidationError",
    "evaluate_condition",
    "find_matching_policy",
    "validate_condition_expr",
]


class ConditionValidationError(ValueError):
    """A condition expression is structurally invalid (write-time check).

    Distinct from the *runtime* fail-closed behaviour of
    :func:`evaluate_condition`: at runtime a malformed expression errs toward
    "require approval" so a broken policy can never silently allow a call. But
    when a policy is *created or edited* (the CRUD surface), we reject the
    malformed shape up front with a precise message, so the operator fixes the
    typo instead of unknowingly shipping a policy that approval-gates
    everything. The two checks are deliberately separate: the gate stays
    fail-closed; the editor stays strict.
    """


@dataclass(frozen=True)
class ApprovalPolicy:
    """A tenant approval policy as the matcher needs to see it.

    A plain value object so the pure matcher never touches the DB. The
    persistence layer (``rolemesh.db.approval``) builds these from rows;
    the orchestrator and the container hook both match against them.
    """

    id: str
    tenant_id: str
    mcp_server_name: str
    tool_name: str           # exact tool name, or "*" for server-wide
    condition_expr: dict[str, Any]
    enabled: bool
    priority: int
    updated_at: datetime
    # Set from the row by the persistence layer; the matcher never reads it.
    # Defaulted so the in-container snapshot builder (which has no created_at)
    # and matcher tests need not supply it.
    created_at: datetime | None = None


# Binary leaf operators. Each takes (actual_field_value, policy_value) and
# may raise — the caller treats any raise as fail-closed (require approval).
def _op_eq(a: Any, b: Any) -> bool:
    return bool(a == b)


def _op_ne(a: Any, b: Any) -> bool:
    return bool(a != b)


def _op_gt(a: Any, b: Any) -> bool:
    return bool(a > b)


def _op_ge(a: Any, b: Any) -> bool:
    return bool(a >= b)


def _op_lt(a: Any, b: Any) -> bool:
    return bool(a < b)


def _op_le(a: Any, b: Any) -> bool:
    return bool(a <= b)


def _op_in(a: Any, b: Any) -> bool:
    # ``a in b`` — the field value is one of the policy's listed values.
    # A plain string ``b`` would do substring matching, which is almost
    # never what an "in [list]" policy means; require a real collection.
    if isinstance(b, (str, bytes)) or not _is_collection(b):
        raise TypeError("'in' expects a list/collection value")
    return a in b


def _op_not_in(a: Any, b: Any) -> bool:
    return not _op_in(a, b)


def _op_contains(a: Any, b: Any) -> bool:
    # ``b in a`` — the field value (a string or collection) contains the
    # policy's value. ``"x" in 123`` raises → fail-closed at the caller.
    return b in a


def _is_collection(v: Any) -> bool:
    """True for list/tuple/set — the shapes ``in``/``not_in`` accept.

    Strings are deliberately excluded (handled by the callers) so that
    ``op: in`` never silently degrades to substring matching.
    """
    return isinstance(v, (list, tuple, set, frozenset))


_OPS = {
    "==": _op_eq,
    "!=": _op_ne,
    ">": _op_gt,
    ">=": _op_ge,
    "<": _op_lt,
    "<=": _op_le,
    "in": _op_in,
    "not_in": _op_not_in,
    "contains": _op_contains,
}

# Sentinel distinguishing "field absent" from "field present and == None".
# A missing field is a fail-closed trigger; a present null is a real value.
_MISSING = object()


def _eval(expr: Any, params: dict[str, Any]) -> bool:
    """Recursively evaluate one node. May raise — callers fail closed.

    Raising (rather than returning ``False``) on anything malformed is
    deliberate: the single top-level ``try`` in :func:`evaluate_condition`
    converts every failure into "approval required", so a broken branch
    can never silently let a call through.
    """
    if not isinstance(expr, dict):
        raise TypeError(f"condition node must be a dict, got {type(expr).__name__}")

    if "always" in expr:
        val = expr["always"]
        if not isinstance(val, bool):
            raise TypeError("'always' must be a bool")
        return val

    if "and" in expr:
        subs = expr["and"]
        if not isinstance(subs, list) or not subs:
            raise ValueError("'and' must be a non-empty list")
        return all(_eval(s, params) for s in subs)

    if "or" in expr:
        subs = expr["or"]
        if not isinstance(subs, list) or not subs:
            raise ValueError("'or' must be a non-empty list")
        return any(_eval(s, params) for s in subs)

    # Leaf: {"field", "op", "value"}.
    field = expr["field"]   # KeyError → fail closed
    op = expr["op"]
    value = expr["value"]
    fn = _OPS[op]           # KeyError on unknown op → fail closed
    actual = params.get(field, _MISSING)
    if actual is _MISSING:
        raise KeyError(f"field {field!r} missing from params")
    return bool(fn(actual, value))


def evaluate_condition(expr: dict[str, Any], params: dict[str, Any]) -> bool:
    """Return whether ``expr`` matches ``params`` — fail-closed.

    ``True`` means the policy condition is satisfied (this call needs
    approval). Any malformed expression, missing field, type mismatch, or
    unexpected exception also returns ``True`` (§7 fail-closed). Only a
    cleanly-evaluated, definitively-false condition returns ``False``.
    """
    try:
        return _eval(expr, params)
    except Exception:  # noqa: BLE001 — fail-closed: any error ⇒ require approval
        return True


def validate_condition_expr(expr: Any, *, _depth: int = 0) -> None:
    """Raise :class:`ConditionValidationError` if ``expr`` is not a well-formed
    condition. Returns ``None`` for a valid expression.

    This is the strict, write-time companion to :func:`evaluate_condition`
    (which is lenient/fail-closed at match time). The accepted grammar is the
    §7 structured language and nothing else::

        {"always": <bool>}
        {"field": <str>, "op": <known op>, "value": <any>}
        {"and": [<expr>, ...]}    # non-empty
        {"or":  [<expr>, ...]}    # non-empty

    A node must be a dict carrying **exactly** the keys of one form — a dict
    that mixes forms (``{"always": true, "field": "x"}``) or carries unknown
    keys is rejected, even though the runtime evaluator would have silently
    taken the first branch. Nesting is bounded (``_MAX_DEPTH``) so a hostile
    or accidental deeply-nested payload can't blow the Python recursion limit
    on the way in.
    """
    if _depth > _MAX_CONDITION_DEPTH:
        raise ConditionValidationError(
            f"condition nesting exceeds {_MAX_CONDITION_DEPTH} levels"
        )
    if not isinstance(expr, dict):
        raise ConditionValidationError(
            f"condition node must be an object, got {type(expr).__name__}"
        )
    keys = set(expr)

    if "always" in keys:
        if keys != {"always"}:
            raise ConditionValidationError(
                "'always' node must carry only the 'always' key"
            )
        if not isinstance(expr["always"], bool):
            raise ConditionValidationError("'always' must be a boolean")
        return

    if "and" in keys or "or" in keys:
        connective = "and" if "and" in keys else "or"
        if keys != {connective}:
            raise ConditionValidationError(
                f"'{connective}' node must carry only the '{connective}' key"
            )
        subs = expr[connective]
        if not isinstance(subs, list) or not subs:
            raise ConditionValidationError(
                f"'{connective}' must be a non-empty list of conditions"
            )
        for sub in subs:
            validate_condition_expr(sub, _depth=_depth + 1)
        return

    # Otherwise it must be a leaf comparison.
    if keys != {"field", "op", "value"}:
        raise ConditionValidationError(
            "leaf condition must carry exactly 'field', 'op', and 'value'"
        )
    if not isinstance(expr["field"], str) or not expr["field"]:
        raise ConditionValidationError("'field' must be a non-empty string")
    if expr["op"] not in _OPS:
        raise ConditionValidationError(
            f"unknown op {expr['op']!r}; expected one of {sorted(_OPS)}"
        )


# Defence-in-depth bound on condition nesting at write time. Real policies are
# shallow; anything past this is almost certainly an error or an attack.
_MAX_CONDITION_DEPTH = 32


def find_matching_policy(
    policies: Sequence[ApprovalPolicy],
    *,
    mcp_server_name: str,
    tool_name: str,
    params: dict[str, Any],
) -> ApprovalPolicy | None:
    """Pick the policy that governs this tool call, or ``None``.

    A policy matches when it is enabled, its ``mcp_server_name`` equals the
    call's server, its ``tool_name`` is ``"*"`` or the exact tool, and its
    condition evaluates true (fail-closed). Among matches, the highest
    ``priority`` wins; ties break to the newest ``updated_at`` (§7).
    """
    candidates = [
        p
        for p in policies
        if p.enabled
        and p.mcp_server_name == mcp_server_name
        and (p.tool_name == "*" or p.tool_name == tool_name)
        and evaluate_condition(p.condition_expr, params)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.priority, p.updated_at))
