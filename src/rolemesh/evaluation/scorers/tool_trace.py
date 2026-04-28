"""tool_trace scorer — required / forbidden / expected_order.

v1 covers the call-shape questions ("did the agent call the
right tools, in the right order, without touching forbidden ones?")
without arg validation. ``arg_checks`` is deferred to v1.1 — the wire
format only carries a short ``input_preview`` for each tool call, so
hooking jmespath against the full args dict needs an upstream
``ToolUseEvent`` change.

Tool name comparison is case-insensitive: Claude SDK uses ``Bash``,
``Read``; Pi uses ``bash``, ``read``. Datasets are written in lower
case; the lower-casing happens here so the wire format stays untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    accuracy,
    scorer,
    stderr,
)

if TYPE_CHECKING:
    from inspect_ai.scorer import Target
    from inspect_ai.solver import TaskState


def _spec(state: TaskState) -> dict[str, Any] | None:
    scoring = state.metadata.get("scoring")
    if not isinstance(scoring, dict):
        return None
    tt = scoring.get("tool_trace")
    return tt if isinstance(tt, dict) else None


def _normalize(name: str) -> str:
    return name.strip().lower()


def _lcs_match_length(observed: list[str], expected: list[str]) -> int:
    """Length of the longest common subsequence of ``expected`` inside
    ``observed`` — i.e. how many of the required steps appeared in
    order, allowing other tool calls in between.

    Standard DP; both inputs are short (single-digit lengths in
    practice) so the O(n*m) table is fine.
    """
    n, m = len(observed), len(expected)
    if n == 0 or m == 0:
        return 0
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if observed[i - 1] == expected[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[n][m]


def _str_list(spec: dict[str, Any], key: str) -> list[str]:
    val = spec.get(key, [])
    if not isinstance(val, list):
        return []
    return [_normalize(x) for x in val if isinstance(x, str)]


@scorer(metrics=[accuracy(), stderr()])
def tool_trace_scorer() -> Scorer:
    """Pass/fail on tool-call shape.

    Samples without ``scoring.tool_trace`` are marked CORRECT (no
    requirement = no failure mode) — keeps tool_trace optional per
    sample without forcing the dataset writer to opt out explicitly.
    """

    async def score(state: TaskState, target: Target) -> Score:
        spec = _spec(state)
        if spec is None:
            return Score(value=CORRECT, explanation="no tool_trace spec")

        observed_raw = state.metadata.get("observed_tool_calls") or []
        observed = [
            _normalize(t) for t in observed_raw
            if isinstance(t, str) and t.strip()
        ]

        forbidden = _str_list(spec, "forbidden_tools")
        required = _str_list(spec, "required_tools")
        expected = _str_list(spec, "expected_order")

        # Forbidden hit dominates everything else — even if every other
        # check would pass, calling a forbidden tool is a fail. Reported
        # first so the explanation is actionable.
        forbidden_hits = sorted(set(observed) & set(forbidden))
        if forbidden_hits:
            return Score(
                value=INCORRECT,
                answer=", ".join(observed),
                explanation=f"forbidden tools called: {forbidden_hits}",
            )

        missing = [r for r in required if r not in observed]
        if missing:
            return Score(
                value=INCORRECT,
                answer=", ".join(observed),
                explanation=f"required tools missing: {missing}",
            )

        if expected:
            matched = _lcs_match_length(observed, expected)
            if matched < len(expected):
                return Score(
                    value=INCORRECT,
                    answer=", ".join(observed),
                    explanation=(
                        f"expected_order partial match: {matched} / "
                        f"{len(expected)} ({expected})"
                    ),
                )

        return Score(value=CORRECT, answer=", ".join(observed))

    return score
