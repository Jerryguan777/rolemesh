"""answer_check — per-rubric model_graded_qa with fraction aggregation.

The grading body is entirely Inspect's ``model_graded_qa``: each rubric
in the sample's ``target`` list is judged by one independent, binary
model_graded_qa call — the most reliable shape for an LLM judge — and
passing a list of judge models upstream would give majority voting per
rubric.

The shell owns three things:

* aggregation: ``value`` is the fraction of rubrics satisfied, so a
  4/5 reply is distinguishable from a 0/5 one (attribution + mean
  resolution) while the pass^k reducers still binarize at 1.0 — only
  an all-rubrics-satisfied trial counts for the consistency gate. A
  single-rubric sample degenerates to 1.0/0.0, preserving the old
  binary semantics.
* itemized explanation: one line per rubric with the judge's verdict,
  so a failed sample names the rubric it missed without opening the
  grading transcripts (which are kept in Score.metadata).
* failure semantics: any judge-call exception (API outage, auth
  failure) makes the whole sample NOANSWER — a partial rubric count
  over a half-graded sample is not evidence, and grading-infra
  failure must never read as agent failure.

Known upstream semantic, deliberately kept: a judge reply with no
parseable ``GRADE:`` label scores that rubric INCORRECT (reply kept in
the explanation). The default instructions make that rare; revisit if
transcripts show otherwise.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from inspect_ai.scorer import (
    CORRECT,
    NOANSWER,
    Score,
    Scorer,
    Target,
    accuracy,
    model_graded_qa,
    scorer,
    stderr,
)

if TYPE_CHECKING:
    from inspect_ai.solver import TaskState

_DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-5"


@scorer(metrics=[accuracy(), stderr()])
def answer_check(judge_model: str | None = None) -> Scorer:
    """Judge the final reply against the sample's ``target`` rubrics.

    judge_model overrides ``EVAL_JUDGE_MODEL`` env / default. Resolved
    once at scorer construction so the choice is logged in the .eval
    file alongside the rest of the run config.
    """
    resolved_judge = (
        judge_model
        or os.environ.get("EVAL_JUDGE_MODEL")
        or _DEFAULT_JUDGE_MODEL
    )
    graded = model_graded_qa(model=resolved_judge)

    async def score(state: TaskState, target: Target) -> Score:
        rubrics = [t for t in target if t.strip()]
        if not rubrics:
            return Score(
                value=NOANSWER,
                explanation="sample has no judge rubrics",
            )

        satisfied = 0
        lines: list[str] = []
        gradings: list[dict[str, Any]] = []
        for idx, rubric in enumerate(rubrics):
            try:
                verdict = await graded(state, Target(rubric))
            except Exception as exc:  # noqa: BLE001 — judge errors must not crash run
                # A partial rubric count over a half-judged sample is
                # not evidence; the whole sample goes ungraded.
                return Score(
                    value=NOANSWER,
                    explanation=(
                        f"judge call failed on rubric [{idx + 1}/"
                        f"{len(rubrics)}]: {exc!r}"
                    ),
                )
            ok = verdict.value == CORRECT
            satisfied += int(ok)
            lines.append(
                f"[{idx + 1}/{len(rubrics)}] "
                f"{'PASS' if ok else 'FAIL'} {rubric}",
            )
            gradings.append({
                "rubric": rubric,
                "grade": verdict.value,
                "explanation": verdict.explanation,
            })

        return Score(
            value=satisfied / len(rubrics),
            answer=state.output.completion,
            explanation="\n".join(lines),
            metadata={"rubrics": gradings},
        )

    return score
