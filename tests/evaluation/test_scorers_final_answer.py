"""final_answer scorer — exact / regex / llm_judge tests.

The mutation-testing posture (replace ``<`` with ``<=``, swap ``and``
with ``or``, drop the ``re.DOTALL`` flag) drives the case selection:
each test asserts the boundary the scorer is supposed to enforce, so
a mutation that softens or inverts the rule fails at least one
assertion.
"""

from __future__ import annotations

from typing import Any

import pytest
from inspect_ai.model import ModelName, ModelOutput
from inspect_ai.scorer import CORRECT, INCORRECT, NOANSWER, Target
from inspect_ai.solver import TaskState

from rolemesh.evaluation.scorers.final_answer import final_answer_scorer


def _state(*, completion: str, scoring: dict[str, Any], input_text: str = "q") -> TaskState:
    return TaskState(
        model=ModelName("test/test"),
        sample_id="s1",
        epoch=0,
        input=input_text,
        messages=[],
        metadata={"scoring": scoring},
        output=ModelOutput.from_content("test/test", completion),
    )


@pytest.mark.asyncio
async def test_exact_matches_after_strip() -> None:
    scorer = final_answer_scorer()
    state = _state(
        completion="  4 \n",
        scoring={"final_answer": {"mode": "exact", "target": "4"}},
    )
    score = await scorer(state, Target("4"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_exact_case_sensitive_by_design() -> None:
    """Two strings differing only in case are NOT equal under exact —
    if a dataset wants case-insensitive, regex with ``(?i)`` is the
    documented escape hatch. Test guards against an accidental
    .lower() mutation."""
    scorer = final_answer_scorer()
    state = _state(
        completion="Paris",
        scoring={"final_answer": {"mode": "exact", "target": "paris"}},
    )
    score = await scorer(state, Target("paris"))
    assert score.value == INCORRECT


@pytest.mark.asyncio
async def test_exact_empty_completion_is_incorrect() -> None:
    """No reply from agent must score INCORRECT — silent failure
    masquerading as success would inflate accuracy."""
    scorer = final_answer_scorer()
    state = _state(
        completion="",
        scoring={"final_answer": {"mode": "exact", "target": "anything"}},
    )
    score = await scorer(state, Target("anything"))
    assert score.value == INCORRECT


@pytest.mark.asyncio
async def test_regex_dotall_spans_newlines() -> None:
    """A pattern like ``answer.*42`` must match across line breaks —
    real LLM output is multi-paragraph. Guards against a mutation
    that drops re.DOTALL."""
    scorer = final_answer_scorer()
    state = _state(
        completion="The answer is\nfinally\n42",
        scoring={"final_answer": {"mode": "regex", "pattern": r"answer.*42"}},
    )
    score = await scorer(state, Target("regex"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_regex_no_match_is_incorrect() -> None:
    scorer = final_answer_scorer()
    state = _state(
        completion="quite the wrong reply",
        scoring={"final_answer": {"mode": "regex", "pattern": r"\bcorrect\b"}},
    )
    score = await scorer(state, Target("regex"))
    assert score.value == INCORRECT


@pytest.mark.asyncio
async def test_invalid_regex_yields_noanswer() -> None:
    """Operator wrote a bad regex: surface NOANSWER (drop from
    accuracy denominator) rather than silently scoring INCORRECT —
    it's an authoring bug, not an agent failure."""
    scorer = final_answer_scorer()
    state = _state(
        completion="anything",
        scoring={"final_answer": {"mode": "regex", "pattern": r"["}},
    )
    score = await scorer(state, Target("regex"))
    assert score.value == NOANSWER


@pytest.mark.asyncio
async def test_unknown_mode_yields_noanswer() -> None:
    """Loader rejects unknown modes upstream, but if a buggy solver
    rewrites metadata we should still fail loud rather than silent-pass."""
    scorer = final_answer_scorer()
    state = _state(
        completion="anything",
        scoring={"final_answer": {"mode": "fuzzy"}},
    )
    score = await scorer(state, Target("?"))
    assert score.value == NOANSWER


@pytest.mark.asyncio
async def test_llm_judge_correct_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the model so the judge returns ``GRADE: C``."""

    class _StubModel:
        async def generate(self, prompt: str) -> Any:
            class _R:
                completion = "Looks good. GRADE: C"
            return _R()

    def _get_model(name: str) -> Any:
        return _StubModel()

    monkeypatch.setattr(
        "rolemesh.evaluation.scorers.final_answer.get_model", _get_model,
    )
    scorer = final_answer_scorer(judge_model="stub/model")
    state = _state(
        completion="42",
        scoring={
            "final_answer": {
                "mode": "llm_judge",
                "criterion": "answer is the meaning of life",
            }
        },
    )
    score = await scorer(state, Target("criterion"))
    assert score.value == CORRECT


@pytest.mark.asyncio
async def test_llm_judge_incorrect_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StubModel:
        async def generate(self, prompt: str) -> Any:
            class _R:
                completion = "Wrong. GRADE: I"
            return _R()

    monkeypatch.setattr(
        "rolemesh.evaluation.scorers.final_answer.get_model",
        lambda _name: _StubModel(),
    )
    scorer = final_answer_scorer(judge_model="stub/model")
    state = _state(
        completion="bad",
        scoring={"final_answer": {"mode": "llm_judge", "criterion": "must be 42"}},
    )
    score = await scorer(state, Target("criterion"))
    assert score.value == INCORRECT


@pytest.mark.asyncio
async def test_llm_judge_no_grade_yields_noanswer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Judge returned text without GRADE marker — must not silently
    treat it as correct OR incorrect."""

    class _StubModel:
        async def generate(self, prompt: str) -> Any:
            class _R:
                completion = "I refuse to grade this"
            return _R()

    monkeypatch.setattr(
        "rolemesh.evaluation.scorers.final_answer.get_model",
        lambda _name: _StubModel(),
    )
    scorer = final_answer_scorer(judge_model="stub/model")
    state = _state(
        completion="x",
        scoring={"final_answer": {"mode": "llm_judge", "criterion": "y"}},
    )
    score = await scorer(state, Target("criterion"))
    assert score.value == NOANSWER


@pytest.mark.asyncio
async def test_llm_judge_call_failure_yields_noanswer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network / auth failure on the judge call must not crash the
    eval — accumulate as NOANSWER and let the operator notice."""

    def _boom(_name: str) -> Any:
        raise RuntimeError("auth failed")

    monkeypatch.setattr(
        "rolemesh.evaluation.scorers.final_answer.get_model", _boom,
    )
    scorer = final_answer_scorer(judge_model="stub/model")
    state = _state(
        completion="x",
        scoring={"final_answer": {"mode": "llm_judge", "criterion": "y"}},
    )
    score = await scorer(state, Target("criterion"))
    assert score.value == NOANSWER
