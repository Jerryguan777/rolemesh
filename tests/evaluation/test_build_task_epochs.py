"""build_eval_task epochs wiring.

epochs == 1 must not pass an Epochs at all (bit-for-bit single-trial
behavior); epochs > 1 must run every sample N times with two reducers
(mean + at_least_N).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("inspect_ai")

from rolemesh.evaluation.dataset import (
    Assertion,
    Dataset,
    Probe,
    Sample,
    StateCheckSpec,
)
from rolemesh.evaluation.inspect_glue import build_eval_task


def _dataset(*, state_check: bool = False) -> Dataset:
    spec = (
        StateCheckSpec(probes=[Probe(
            url="https://s/api/x",
            assertions=[Assertion(path="status", op="exists")],
        )])
        if state_check else None
    )
    return Dataset(
        path="/tmp/ds.jsonl",
        sha256="0" * 64,
        samples=[
            Sample(
                id="q1",
                input="hello",
                target=["the reply greets the user"],
                state_check=spec,
            ),
        ],
    )


def _build(epochs: int):
    return build_eval_task(
        dataset=_dataset(),
        runner=SimpleNamespace(),  # only captured in the solver closure
        coworker=SimpleNamespace(),
        epochs=epochs,
    )


def test_single_epoch_passes_no_epochs() -> None:
    task = _build(1)
    assert task.epochs is None
    assert task.epochs_reducer is None


def test_multi_epoch_sets_count_and_two_reducers() -> None:
    task = _build(5)
    assert task.epochs == 5
    # mean + at_least(5): per-trial pass rate and all-trials-passed
    # rate. Two reducers — a count mutation here means the .eval log
    # loses one of the two headline metrics.
    assert len(task.epochs_reducer or []) == 2


def test_state_dataset_gets_two_scorers_and_spec_in_metadata() -> None:
    """Homogeneous state datasets attach the state axis; plain QA
    datasets must not carry a filler state column."""
    plain = build_eval_task(
        dataset=_dataset(), runner=SimpleNamespace(),
        coworker=SimpleNamespace(),
    )
    assert len(plain.scorer or []) == 1

    stateful = build_eval_task(
        dataset=_dataset(state_check=True), runner=SimpleNamespace(),
        coworker=SimpleNamespace(),
    )
    assert len(stateful.scorer or []) == 2
    sample = next(iter(stateful.dataset))
    spec = (sample.metadata or {}).get("state_check")
    # Serialized spec travels in metadata into the .eval log so the
    # scorer (and offline re-scoring) can read it back.
    assert spec is not None
    assert spec["probes"][0]["url"] == "https://s/api/x"
