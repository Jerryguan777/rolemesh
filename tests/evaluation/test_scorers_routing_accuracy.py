"""routing_accuracy scorer tests — frontdesk v1.2.

The contract from handbook §6 Step 8.3 has four rules. These tests
walk through each, plus the multi-delegate edge cases that aren't
trivially derivable from the rule statement:

  1. expected_target=X, observed has delegate_to_agent(target=X) -> CORRECT.
  2. expected_target=X, observed has delegate_to_agent(target=Y) -> INCORRECT.
  3. expected_target=X, observed has no delegate_to_agent -> INCORRECT.
  4. expected_target=null, observed has no delegate_to_agent -> CORRECT.
  Plus:
  5. expected_target=null, observed has delegate_to_agent -> INCORRECT
     (the LLM delegated when it should have answered itself; this is
     the "broadcast greeting to portfolio" failure mode).
  6. expected_target=X, observed has delegate_to_agent(target=X) AND
     delegate_to_agent(target=Y) -> INCORRECT (hedge-by-broadcast: at
     least one right answer is not enough when the wrong fan-out is
     operationally expensive).
  7. expected_target=X, observed has parallel delegate_to_agent(target=X)
     calls (e.g. two questions for the same specialist) -> CORRECT.

Adversarial guards on the spec parser are also tested (non-string
expected_target etc.) so the scorer fails loud rather than silently
mis-scoring on a malformed dataset.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("inspect_ai")

from inspect_ai.model import ModelName, ModelOutput
from inspect_ai.scorer import CORRECT, INCORRECT, Target
from inspect_ai.solver import TaskState

from rolemesh.evaluation.scorers.routing_accuracy import routing_accuracy_scorer


def _state(
    *,
    observed_tool_calls: list[str] | None = None,
    observed_tool_inputs: list[str] | None = None,
    routing: dict[str, Any] | None = None,
) -> TaskState:
    metadata: dict[str, Any] = {}
    if observed_tool_calls is not None:
        metadata["observed_tool_calls"] = observed_tool_calls
    if observed_tool_inputs is not None:
        metadata["observed_tool_inputs"] = observed_tool_inputs
    if routing is not None:
        metadata["scoring"] = {"routing": routing}
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
# Rule 1 — correct target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correct_target_scores_correct() -> None:
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["delegate_to_agent"],
        observed_tool_inputs=["trading"],
        routing={"expected_target": "trading"},
    )
    score = await scorer(state, Target("trading"))
    assert score.value == CORRECT
    assert score.answer == "trading"


@pytest.mark.asyncio
async def test_namespaced_tool_name_normalized() -> None:
    """Pi and Claude SDK both emit ``delegate_to_agent`` after the
    runner strips the ``mcp__rolemesh__`` prefix, but a future MCP
    server layout might leak the prefix into the observed trace. The
    scorer normalizes so the comparison still hits.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["mcp__rolemesh__delegate_to_agent"],
        observed_tool_inputs=["trading"],
        routing={"expected_target": "trading"},
    )
    score = await scorer(state, Target("trading"))
    assert score.value == CORRECT


# ---------------------------------------------------------------------------
# Rule 2 — wrong target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_target_scores_incorrect() -> None:
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["delegate_to_agent"],
        observed_tool_inputs=["portfolio"],
        routing={"expected_target": "trading"},
    )
    score = await scorer(state, Target("trading"))
    assert score.value == INCORRECT
    # The explanation must name the observed-vs-expected delta so the
    # operator can triage in the .eval log without re-running the sample.
    assert score.explanation is not None
    assert "trading" in score.explanation
    assert "portfolio" in score.explanation


# ---------------------------------------------------------------------------
# Rule 3 — expected delegation missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expected_target_but_no_delegate_scores_incorrect() -> None:
    """Frontdesk answered itself when it should have routed — a real
    failure mode for borderline cases like "how am I doing?" where the
    LLM might short-circuit instead of asking portfolio.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["list_agents"],  # only listed, never delegated
        observed_tool_inputs=[""],
        routing={"expected_target": "portfolio"},
    )
    score = await scorer(state, Target("portfolio"))
    assert score.value == INCORRECT


@pytest.mark.asyncio
async def test_expected_target_but_empty_trace_scores_incorrect() -> None:
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=[],
        observed_tool_inputs=[],
        routing={"expected_target": "trading"},
    )
    score = await scorer(state, Target("trading"))
    assert score.value == INCORRECT


# ---------------------------------------------------------------------------
# Rule 4 — no-match case correctly handled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_target_with_no_delegate_scores_correct() -> None:
    """``expected_target=null`` = "frontdesk should answer itself".
    Greetings, capability questions, status checks all fall here.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=[],
        observed_tool_inputs=[],
        routing={"expected_target": None},
    )
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_null_target_with_only_list_agents_scores_correct() -> None:
    """The LLM may call list_agents to refresh its view of the catalog
    even on a simple greeting; that's not a delegation and must not
    be scored as one.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["list_agents"],
        observed_tool_inputs=[""],
        routing={"expected_target": None},
    )
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


# ---------------------------------------------------------------------------
# Rule 5 — spurious delegation on a no-match case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_target_with_unwanted_delegate_scores_incorrect() -> None:
    """The LLM broadcasting "Hi!" to portfolio is the failure mode
    this rule catches. Mutation guard: dropping the expected-null
    spurious-delegate branch would let this pass and the gate would
    happily approve a chatty routing model.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["delegate_to_agent"],
        observed_tool_inputs=["portfolio"],
        routing={"expected_target": None},
    )
    score = await scorer(state, Target("?"))
    assert score.value == INCORRECT
    assert score.explanation is not None
    assert "portfolio" in score.explanation


# ---------------------------------------------------------------------------
# Rule 6 — hedge-by-broadcast must fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_delegate_with_wrong_target_scores_incorrect() -> None:
    """LLM delegated to both trading AND portfolio; trading is right
    but portfolio fan-out is wasted work. The scorer treats this as a
    fail so the eval gate disincentivizes hedging.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["delegate_to_agent", "delegate_to_agent"],
        observed_tool_inputs=["trading", "portfolio"],
        routing={"expected_target": "trading"},
    )
    score = await scorer(state, Target("trading"))
    assert score.value == INCORRECT
    # Explanation must surface the WRONG target so the operator knows
    # which fan-out to fix in the system prompt.
    assert score.explanation is not None
    assert "portfolio" in score.explanation


# ---------------------------------------------------------------------------
# Rule 7 — parallel calls to the same specialist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_delegate_all_correct_target_scores_correct() -> None:
    """Two questions for the same specialist in parallel is a valid
    pattern (handbook decision #6); it should NOT count against the
    score even though there were two delegations.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["delegate_to_agent", "delegate_to_agent"],
        observed_tool_inputs=["trading", "trading"],
        routing={"expected_target": "trading"},
    )
    score = await scorer(state, Target("trading"))
    assert score.value == CORRECT


# ---------------------------------------------------------------------------
# Defensive — opt-in posture and malformed specs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_routing_spec_scores_correct() -> None:
    """Datasets without a routing spec opt out — the scorer awards
    CORRECT so unrelated samples in the same run don't drag the
    accuracy down.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=["delegate_to_agent"],
        observed_tool_inputs=["trading"],
        routing=None,
    )
    score = await scorer(state, Target("?"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_malformed_expected_target_scores_incorrect_with_reason() -> None:
    """A dataset bug (non-string expected_target) MUST fail the
    sample with a clear explanation. Silently scoring CORRECT would
    inflate accuracy on a broken dataset.
    """
    scorer = routing_accuracy_scorer()
    state = _state(
        observed_tool_calls=[],
        observed_tool_inputs=[],
        routing={"expected_target": 42},  # bug — int, not str/None
    )
    score = await scorer(state, Target("?"))
    assert score.value == INCORRECT
    assert score.explanation is not None
    assert "expected_target" in score.explanation
