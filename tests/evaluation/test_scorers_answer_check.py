"""answer_check scorer — per-rubric aggregation and failure semantics.

The inner model_graded_qa is stubbed at the module seam; each test
drives the shell's own responsibilities: fraction arithmetic, the
single-rubric degenerate case, and exception → NOANSWER.
"""

from __future__ import annotations

from typing import Any

import pytest

# The eval extra (inspect-ai) is nightly-only: PR CI runs without it.
pytest.importorskip("inspect_ai")

import importlib

from inspect_ai.model import ModelName, ModelOutput
from inspect_ai.scorer import CORRECT, INCORRECT, NOANSWER, Score, Target
from inspect_ai.solver import TaskState

# The package re-exports the answer_check *function* under the same
# name as its module; go through sys.modules for the real module.
ac_mod = importlib.import_module("rolemesh.evaluation.scorers.answer_check")


def _state(completion: str = "done") -> TaskState:
    return TaskState(
        model=ModelName("test/test"),
        sample_id="s1",
        epoch=1,
        input="do the thing",
        messages=[],
        metadata={},
        output=ModelOutput.from_content("test/test", completion),
    )


def _stub_judge(verdicts: dict[str, str], calls: list[str]):
    """Replace model_graded_qa with a factory whose scorer grades each
    rubric by lookup; a rubric mapped to 'raise' throws."""

    def factory(**_kw: Any):
        async def graded(state: TaskState, target: Target) -> Score:
            rubric = next(iter(target))
            calls.append(rubric)
            if verdicts.get(rubric) == "raise":
                msg = "judge api down"
                raise RuntimeError(msg)
            value = CORRECT if verdicts.get(rubric) == "C" else INCORRECT
            return Score(value=value, explanation=f"judged {rubric!r}")

        return graded

    return factory


@pytest.mark.asyncio
async def test_fraction_of_rubrics_satisfied(monkeypatch) -> None:
    """2 of 3 rubrics pass → 2/3, with an itemized PASS/FAIL line per
    rubric. A mutation to binary scoring or short-circuiting breaks
    this."""
    calls: list[str] = []
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge(
        {"r1": "C", "r2": "I", "r3": "C"}, calls,
    ))
    score = await ac_mod.answer_check()(
        _state(), Target(["r1", "r2", "r3"]),
    )
    assert score.value == pytest.approx(2 / 3)
    # Every rubric judged independently, in order.
    assert calls == ["r1", "r2", "r3"]
    assert "[2/3] FAIL r2" in (score.explanation or "")
    assert len((score.metadata or {})["rubrics"]) == 3


@pytest.mark.asyncio
async def test_single_rubric_degenerates_to_binary(monkeypatch) -> None:
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge(
        {"r1": "C"}, [],
    ))
    score = await ac_mod.answer_check()(_state(), Target(["r1"]))
    assert score.value == 1.0
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge(
        {"r1": "I"}, [],
    ))
    score = await ac_mod.answer_check()(_state(), Target(["r1"]))
    assert score.value == 0.0


@pytest.mark.asyncio
async def test_judge_exception_is_noanswer_for_whole_sample(monkeypatch) -> None:
    """A half-judged sample is not evidence: rubric 1 passing must not
    surface as a 1/3 partial when rubric 2's judge call died."""
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge(
        {"r1": "C", "r2": "raise", "r3": "C"}, [],
    ))
    score = await ac_mod.answer_check()(
        _state(), Target(["r1", "r2", "r3"]),
    )
    assert score.value == NOANSWER
    assert "rubric [2/3]" in (score.explanation or "")


@pytest.mark.asyncio
async def test_empty_rubrics_is_noanswer(monkeypatch) -> None:
    """Loader forbids this; reaching it means glue breakage, and a
    divide-by-zero or a free CORRECT would both be worse."""
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge({}, []))
    score = await ac_mod.answer_check()(_state(), Target([" "]))
    assert score.value == NOANSWER
