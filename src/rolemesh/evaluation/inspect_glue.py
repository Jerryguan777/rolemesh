"""Adapt EvalRunner + dataset to the Inspect AI Task interface.

Inspect's Task drives parallelism, log writing (.eval files), and
metric aggregation. Our solver is a thin wrapper that delegates to
``EvalRunner.execute_sample`` and copies its results into ``TaskState``
so the scorers can read them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset
from inspect_ai.dataset import Sample as InspectSample
from inspect_ai.solver import Generate, Solver, TaskState, solver

from rolemesh.evaluation.scorers import final_answer_scorer, tool_trace_scorer

if TYPE_CHECKING:
    from rolemesh.core.types import Coworker
    from rolemesh.evaluation.dataset import Dataset, Sample
    from rolemesh.evaluation.runner import EvalRunner


def _sample_to_inspect(sample: Sample, sample_idx: int) -> InspectSample:
    """Convert our Sample to an Inspect Sample with metadata payload.

    target is set to a stable string (the criterion / pattern / target
    text, depending on mode) so the .eval file's per-sample summary
    surfaces it in Inspect's UI without us reaching into metadata.
    """
    fa = sample.final_answer
    if fa.mode == "exact":
        target = fa.target or ""
    elif fa.mode == "regex":
        target = f"regex: {fa.pattern}"
    else:
        target = f"criterion: {fa.criterion}"

    metadata: dict[str, Any] = {
        "sample_idx": sample_idx,
        "scoring": {
            "final_answer": {
                "mode": fa.mode,
                "target": fa.target,
                "pattern": fa.pattern,
                "criterion": fa.criterion,
            },
        },
    }
    if sample.tool_trace is not None:
        metadata["scoring"]["tool_trace"] = {
            "required_tools": list(sample.tool_trace.required_tools),
            "forbidden_tools": list(sample.tool_trace.forbidden_tools),
            "expected_order": list(sample.tool_trace.expected_order),
        }
    if sample.metadata:
        metadata["sample_metadata"] = dict(sample.metadata)

    return InspectSample(
        id=sample.id,
        input=sample.input,
        target=target,
        metadata=metadata,
    )


@solver
def container_solver(runner: EvalRunner, coworker: Coworker) -> Solver:
    """Solver that ships each sample through the production container.

    Populates ``state.output.completion`` so default scorers see the
    final reply text, and stuffs the structured execution result under
    ``state.metadata`` for the eval-specific scorers to read.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample_idx = int(state.metadata.get("sample_idx", 0))
        prompt_raw = state.input
        prompt = (
            prompt_raw if isinstance(prompt_raw, str) else str(prompt_raw)
        )
        execution = await runner.execute_sample(
            coworker=coworker, sample_idx=sample_idx, prompt=prompt,
        )
        state.output.completion = execution.output_text
        state.metadata["observed_tool_calls"] = list(execution.observed_tool_calls)
        state.metadata["usage"] = execution.usage
        state.metadata["latency_ms"] = execution.latency_ms
        state.metadata["sample_status"] = execution.status
        if execution.error:
            state.metadata["sample_error"] = execution.error
        if execution.result_event_count != 1:
            # Single-prompt eval samples should produce exactly one
            # final ResultEvent. Anything else (zero = no reply, >1 =
            # batched / state pollution) is worth flagging in the .eval
            # log so an operator inspecting results sees it.
            state.metadata["result_event_count"] = execution.result_event_count
        if execution.metadata:
            state.metadata["execution_metadata"] = dict(execution.metadata)
        return state

    return solve


def build_eval_task(
    *,
    dataset: Dataset,
    runner: EvalRunner,
    coworker: Coworker,
    judge_model: str | None = None,
    task_name: str = "rolemesh-eval",
) -> Task:
    """Construct the Inspect AI Task we hand to ``inspect_ai.eval``."""
    inspect_samples = [
        _sample_to_inspect(s, idx) for idx, s in enumerate(dataset.samples)
    ]
    return Task(
        name=task_name,
        dataset=MemoryDataset(samples=inspect_samples),
        solver=container_solver(runner, coworker),
        scorer=[
            final_answer_scorer(judge_model=judge_model),
            tool_trace_scorer(),
        ],
    )
