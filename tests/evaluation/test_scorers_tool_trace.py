"""tool_trace scorer tests.

Cover the four pass/fail modes with the LCS edge cases (off-by-one
on order index, single-element trace, all-permitted-tools dataset,
case mismatch between dataset and observed wire format).
"""

from __future__ import annotations

from typing import Any

import pytest
from inspect_ai.model import ModelName, ModelOutput
from inspect_ai.scorer import CORRECT, INCORRECT, Target
from inspect_ai.solver import TaskState

from rolemesh.evaluation.scorers.tool_trace import (
    _lcs_match_length,
    tool_trace_scorer,
)


def _state(*, observed: list[str], spec: dict[str, Any] | None) -> TaskState:
    metadata: dict[str, Any] = {"observed_tool_calls": observed}
    if spec is not None:
        metadata["scoring"] = {"tool_trace": spec}
    return TaskState(
        model=ModelName("test/test"),
        sample_id="s1",
        epoch=0,
        input="q",
        messages=[],
        metadata=metadata,
        output=ModelOutput.from_content("test/test", ""),
    )


# ---------------------------------------------------------------------------
# LCS algorithm
# ---------------------------------------------------------------------------


def test_lcs_full_match_in_order() -> None:
    assert _lcs_match_length(["a", "b", "c"], ["a", "b", "c"]) == 3


def test_lcs_partial_match_returns_count() -> None:
    """Two of three required steps appear, the third is missing —
    score should reflect the partial match length, not collapse to 0."""
    assert _lcs_match_length(["a", "x", "b"], ["a", "b", "c"]) == 2


def test_lcs_other_tools_in_between_dont_break_order() -> None:
    """The contract is subsequence, not substring — interspersed
    unrelated tool calls don't break the order check."""
    assert _lcs_match_length(["a", "noise", "b", "more", "c"], ["a", "b", "c"]) == 3


def test_lcs_wrong_order_partial_credit() -> None:
    """When tools appear out of order, LCS gives the longest
    in-order subsequence, not full credit."""
    assert _lcs_match_length(["b", "a"], ["a", "b"]) == 1


def test_lcs_empty_inputs() -> None:
    assert _lcs_match_length([], ["a"]) == 0
    assert _lcs_match_length(["a"], []) == 0


def test_lcs_single_element() -> None:
    """Off-by-one guard: single element must produce 1, not 0."""
    assert _lcs_match_length(["a"], ["a"]) == 1
    assert _lcs_match_length(["a"], ["b"]) == 0


# ---------------------------------------------------------------------------
# Scorer behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_spec_means_correct() -> None:
    """Datasets without tool_trace simply opt out — no requirement,
    no failure mode."""
    scorer = tool_trace_scorer()
    state = _state(observed=["bash"], spec=None)
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_required_present_passes() -> None:
    scorer = tool_trace_scorer()
    state = _state(
        observed=["bash", "read"],
        spec={"required_tools": ["read"]},
    )
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_required_missing_fails() -> None:
    scorer = tool_trace_scorer()
    state = _state(
        observed=["bash"],
        spec={"required_tools": ["edit"]},
    )
    score = await scorer(state, Target("?"))
    assert score.value == INCORRECT
    assert "missing" in (score.explanation or "").lower()


@pytest.mark.asyncio
async def test_forbidden_dominates_required() -> None:
    """Even if every required tool was used, calling a forbidden one
    is still a fail. Test guards against an && / || mutation that
    would let either condition pass alone."""
    scorer = tool_trace_scorer()
    state = _state(
        observed=["bash", "rm"],
        spec={"required_tools": ["bash"], "forbidden_tools": ["rm"]},
    )
    score = await scorer(state, Target("?"))
    assert score.value == INCORRECT
    assert "forbidden" in (score.explanation or "").lower()


@pytest.mark.asyncio
async def test_case_insensitive_normalization() -> None:
    """Claude SDK emits ``Bash``, datasets are usually lower-case.
    Scorer must normalize so the same dataset works against either
    backend without rewriting."""
    scorer = tool_trace_scorer()
    state = _state(
        observed=["Bash", "Read"],  # Claude-style PascalCase
        spec={"required_tools": ["bash", "read"]},
    )
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_expected_order_partial_credit_fails() -> None:
    """Two of three expected tools matched in order — design says
    partial = failure (score < required = 0). Guards against a
    ``<=`` mutation that would silently pass partial matches."""
    scorer = tool_trace_scorer()
    state = _state(
        observed=["a", "b"],
        spec={"expected_order": ["a", "b", "c"]},
    )
    score = await scorer(state, Target("?"))
    assert score.value == INCORRECT


@pytest.mark.asyncio
async def test_expected_order_full_match_passes_with_noise() -> None:
    scorer = tool_trace_scorer()
    state = _state(
        observed=["a", "x", "b", "y", "c"],
        spec={"expected_order": ["a", "b", "c"]},
    )
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_empty_observed_with_required_fails() -> None:
    """Agent never called any tool — required spec must fail. Off-by-
    one guard against an "if observed: check" branch that would skip
    the failure path on empty input."""
    scorer = tool_trace_scorer()
    state = _state(observed=[], spec={"required_tools": ["bash"]})
    score = await scorer(state, Target("?"))
    assert score.value == INCORRECT
