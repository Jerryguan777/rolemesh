"""answer_check — Inspect's model_graded_qa with a fail-soft shell.

The grading body is entirely Inspect's ``model_graded_qa``: the judge
criterion comes from ``Sample.target`` (per-sample, set by the dataset
loader), the prompt template and GRADE parsing are upstream's defaults,
and passing a list of judge models upstream gives majority voting.

The shell exists for exactly two reasons:

* an exception from the judge call (API outage, auth failure) must
  surface as ``NOANSWER`` — grading-infrastructure failure is not
  evidence about the agent, and letting it crash the scorer would
  abort the whole sample;
* a stable scorer name (``answer_check``) so threshold specs and the
  metrics summary don't churn if the delegation target ever changes.

Known upstream semantic, deliberately kept: a judge reply that carries
no parseable ``GRADE:`` label scores INCORRECT (with the reply in the
explanation). The default instructions make that rare; revisit if
transcripts show otherwise.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from inspect_ai.scorer import (
    NOANSWER,
    Score,
    Scorer,
    accuracy,
    model_graded_qa,
    scorer,
    stderr,
)

if TYPE_CHECKING:
    from inspect_ai.scorer import Target
    from inspect_ai.solver import TaskState

_DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-5"


@scorer(metrics=[accuracy(), stderr()])
def answer_check(judge_model: str | None = None) -> Scorer:
    """Judge the final reply against the sample's ``target`` criterion.

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
        try:
            return await graded(state, target)
        except Exception as exc:  # noqa: BLE001 — judge errors must not crash run
            return Score(
                value=NOANSWER,
                explanation=f"judge call failed: {exc!r}",
            )

    return score
