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
async def test_judge_none_score_is_noanswer(monkeypatch) -> None:
    """The Scorer protocol allows returning None; the shell must not
    assume model_graded_qa's narrower behavior (AttributeError
    otherwise)."""

    def factory(**_kw: Any):
        async def graded(state: TaskState, target: Target) -> Score | None:
            return None

        return graded

    monkeypatch.setattr(ac_mod, "model_graded_qa", factory)
    score = await ac_mod.answer_check()(_state(), Target(["r1"]))
    assert score.value == NOANSWER
    assert "no score" in (score.explanation or "")


@pytest.mark.asyncio
async def test_empty_rubrics_is_noanswer(monkeypatch) -> None:
    """Loader forbids this; reaching it means glue breakage, and a
    divide-by-zero or a free CORRECT would both be worse."""
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge({}, []))
    score = await ac_mod.answer_check()(_state(), Target([" "]))
    assert score.value == NOANSWER


@pytest.mark.parametrize("completion", ["", "   \n\t "])
@pytest.mark.asyncio
async def test_empty_completion_scores_zero_without_judge(
    monkeypatch, completion: str,
) -> None:
    """An empty reply must fail every rubric with no judge calls: fed
    to model_graded_qa, a blank {answer} next to a fact-rich
    {criterion} can be misread as the submission and score CORRECT
    (observed in the field: 0.75 for a 0-char reply). It is 0.0, not
    NOANSWER — the reply is gradeable, the grading infra is fine."""
    calls: list[str] = []
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge(
        {"r1": "C", "r2": "C"}, calls,
    ))
    score = await ac_mod.answer_check()(
        _state(completion), Target(["r1", "r2"]),
    )
    assert score.value == 0.0
    assert calls == []
    assert "empty completion" in (score.explanation or "")
    gradings = (score.metadata or {})["rubrics"]
    assert [g["rubric"] for g in gradings] == ["r1", "r2"]
    assert all(g["grade"] == INCORRECT for g in gradings)


@pytest.mark.asyncio
async def test_no_rubrics_guard_precedes_empty_completion(monkeypatch) -> None:
    """Both guards firing at once is a dataset/glue problem first:
    NOANSWER (infra signal) must win over the 0.0 agent grade."""
    monkeypatch.setattr(ac_mod, "model_graded_qa", _stub_judge({}, []))
    score = await ac_mod.answer_check()(_state(""), Target([" "]))
    assert score.value == NOANSWER
