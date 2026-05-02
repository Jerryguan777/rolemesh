"""final_answer scorer — exact / regex / llm_judge.

Reads the per-sample scoring spec from ``state.metadata['scoring']`` so
one Task can mix modes across samples — operators usually want
exact-match for deterministic prompts and llm_judge for open-ended
ones in the same dataset.

llm_judge runs a one-shot model call against ``EVAL_JUDGE_MODEL``
(default ``anthropic/claude-sonnet-4-5``). The judge answer is parsed
for ``GRADE: C`` (correct) vs anything else (incorrect). Failures of
the judge call surface as ``Score(value=NOANSWER)`` so they don't
pollute the accuracy denominator silently.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from inspect_ai.model import get_model
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    NOANSWER,
    Score,
    Scorer,
    accuracy,
    scorer,
    stderr,
)

if TYPE_CHECKING:
    from inspect_ai.scorer import Target
    from inspect_ai.solver import TaskState

_DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-5"

_JUDGE_PROMPT = """\
You are grading an AI assistant's answer.

The user asked:
{question}

The assistant answered:
{answer}

Grading criterion:
{criterion}

Return GRADE: C if the answer satisfies the criterion, GRADE: I otherwise.
Briefly explain in one sentence on the line before GRADE.
"""

_GRADE_RE = re.compile(r"GRADE:\s*([CI])", re.IGNORECASE)


def _spec(state: TaskState) -> dict[str, Any]:
    scoring = state.metadata.get("scoring")
    if not isinstance(scoring, dict):
        return {}
    fa = scoring.get("final_answer")
    return fa if isinstance(fa, dict) else {}


async def _grade_with_judge(
    *, question: str, answer: str, criterion: str, judge_model: str
) -> Score:
    """One-shot judge call. NOANSWER on infrastructure failure."""
    try:
        model = get_model(judge_model)
        prompt = _JUDGE_PROMPT.format(
            question=question, answer=answer, criterion=criterion,
        )
        result = await model.generate(prompt)
        text = result.completion or ""
    except Exception as exc:  # noqa: BLE001 — judge errors must not crash run
        return Score(
            value=NOANSWER,
            explanation=f"judge call failed: {exc!r}",
        )
    match = _GRADE_RE.search(text)
    if match is None:
        return Score(
            value=NOANSWER,
            answer=answer,
            explanation=f"judge response did not include GRADE: {text!r}",
        )
    grade = match.group(1).upper()
    return Score(
        value=CORRECT if grade == "C" else INCORRECT,
        answer=answer,
        explanation=text.strip(),
    )


@scorer(metrics=[accuracy(), stderr()])
def final_answer_scorer(judge_model: str | None = None) -> Scorer:
    """Scorer reading the per-sample mode from state.metadata.

    judge_model overrides ``EVAL_JUDGE_MODEL`` env / default. Resolved
    once at scorer construction so the choice is logged in the .eval
    file alongside the rest of the run config.
    """
    resolved_judge = (
        judge_model
        or os.environ.get("EVAL_JUDGE_MODEL")
        or _DEFAULT_JUDGE_MODEL
    )

    async def score(state: TaskState, target: Target) -> Score:
        spec = _spec(state)
        mode = spec.get("mode")
        # ``state.output.completion`` is what the solver wrote; missing
        # means the agent produced no final reply, which we score as
        # incorrect rather than NOANSWER — a non-responsive agent IS
        # failing the task, the framework just can't tell why.
        completion = state.output.completion or ""

        if mode == "exact":
            target_text = spec.get("target") or ""
            ok = completion.strip() == target_text.strip()
            return Score(
                value=CORRECT if ok else INCORRECT,
                answer=completion,
                explanation=(
                    None if ok
                    else f"exact mismatch — expected {target_text!r}"
                ),
            )

        if mode == "regex":
            pattern = spec.get("pattern") or ""
            try:
                # DOTALL matches multi-line completions against single
                # ``.*`` patterns — most real prompts produce multiple
                # paragraphs and a non-DOTALL pattern would surprise.
                ok = re.search(pattern, completion, re.DOTALL) is not None
            except re.error as exc:
                return Score(
                    value=NOANSWER,
                    answer=completion,
                    explanation=f"invalid regex {pattern!r}: {exc}",
                )
            return Score(
                value=CORRECT if ok else INCORRECT,
                answer=completion,
                explanation=(
                    None if ok else f"regex {pattern!r} did not match"
                ),
            )

        if mode == "llm_judge":
            criterion = spec.get("criterion") or ""
            question_raw = state.input
            question = (
                question_raw if isinstance(question_raw, str)
                else str(question_raw)
            )
            return await _grade_with_judge(
                question=question,
                answer=completion,
                criterion=criterion,
                judge_model=resolved_judge,
            )

        # Unknown mode — dataset loader rejects this earlier, so reaching
        # here means the solver injected metadata in a bad shape.
        return Score(
            value=NOANSWER,
            answer=completion,
            explanation=f"unknown final_answer mode: {mode!r}",
        )

    return score
