"""CLI helper logic — threshold parsing and metric aggregation.

The CLI itself is exercised manually; these tests cover the pieces
that make decisions from data (whether the run failed, what the p95
latency was) so a regression there is caught before a nightly run
silently passes a threshold it shouldn't.
"""

from __future__ import annotations

from typing import Any

import pytest

from rolemesh.evaluation.cli import (
    _aggregate_metrics,
    _check_thresholds,
    _percentile,
)

# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def test_percentile_single_value() -> None:
    assert _percentile([7.0], 0.5) == 7.0
    assert _percentile([7.0], 0.95) == 7.0


def test_percentile_p50_p95() -> None:
    """Linear-interpolated inclusive percentile over 0..10."""
    vals = [float(x) for x in range(11)]  # 0..10
    assert _percentile(vals, 0.5) == 5.0
    # p95 over 11 values: k = 10*0.95 = 9.5; lo=9, hi=10 → 9.5
    assert _percentile(vals, 0.95) == 9.5


def test_percentile_empty_returns_none() -> None:
    """Mutation guard: any non-None default would silently turn an
    empty latency list into a misleading ``0.0`` summary."""
    assert _percentile([], 0.5) is None


# ---------------------------------------------------------------------------
# Threshold parsing
# ---------------------------------------------------------------------------


def test_threshold_passes_when_value_meets_bar() -> None:
    metrics = {"scorers": {"answer_check": {"accuracy": 0.91}}}
    assert _check_thresholds(
        metrics, ["scorers.answer_check.accuracy>=0.9"]
    ) == []


def test_threshold_fails_when_value_below() -> None:
    metrics = {"scorers": {"answer_check": {"accuracy": 0.85}}}
    failures = _check_thresholds(
        metrics, ["scorers.answer_check.accuracy>=0.9"]
    )
    assert len(failures) == 1
    assert "0.8500" in failures[0]


def test_threshold_boundary_inclusive() -> None:
    """``>=`` is the operator — equality must pass. Mutation guard
    against ``>``."""
    metrics = {"scorers": {"answer_check": {"accuracy": 0.9}}}
    assert _check_thresholds(
        metrics, ["scorers.answer_check.accuracy>=0.9"]
    ) == []


def test_threshold_missing_key_fails_loud() -> None:
    """If the threshold names a metric that doesn't exist, that's a
    config error — surfacing as a violation forces the operator to
    fix the spec rather than silently passing every nightly run."""
    failures = _check_thresholds({}, ["nonexistent.path>=0.5"])
    assert len(failures) == 1
    assert "not present" in failures[0]


def test_threshold_invalid_spec_loud() -> None:
    failures = _check_thresholds({}, ["nonsense"])
    assert len(failures) == 1
    assert "invalid threshold" in failures[0]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class _FakeSample:
    def __init__(self, scores: dict[str, Any] | None = None, **metadata: Any) -> None:
        self.metadata = metadata
        self.scores = scores or {}


class _FakeSampleScore:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeMetric:
    def __init__(self, value: float) -> None:
        self.value = value


class _FakeScore:
    def __init__(
        self, name: str, accuracy: float, reducer: str | None = None,
    ) -> None:
        self.name = name
        self.reducer = reducer
        self.metrics = {"accuracy": _FakeMetric(accuracy)}


class _FakeResults:
    def __init__(self, scores: list[_FakeScore]) -> None:
        self.scores = scores


class _FakeEvalLog:
    def __init__(self, samples: list[_FakeSample], scores: list[_FakeScore]) -> None:
        self.samples = samples
        self.results = _FakeResults(scores)


def test_aggregate_handles_partial_cost_coverage() -> None:
    """When some samples report cost=None (Pi backend gaps),
    cost_usd_total covers only the populated subset and
    cost_usd_coverage exposes the fraction. Mutation guard against
    silently treating None as 0."""
    samples = [
        _FakeSample(latency_ms=100, usage={"cost_usd": 0.01, "input_tokens": 50}),
        _FakeSample(latency_ms=200, usage={"cost_usd": None, "input_tokens": 30}),
        _FakeSample(latency_ms=300, usage={"cost_usd": 0.02, "input_tokens": 70}),
    ]
    log = _FakeEvalLog(
        samples=samples,
        scores=[_FakeScore("answer_check", accuracy=0.66)],
    )
    metrics = _aggregate_metrics(inspect_results=log, sample_count=3)
    assert metrics["cost_usd_total"] == 0.03
    # Coverage: 2 of 3 samples reported cost.
    assert abs(metrics["cost_usd_coverage"] - (2 / 3)) < 1e-6
    assert metrics["tokens"]["input"] == 50 + 30 + 70
    assert metrics["latency_ms"]["p50"] == 200.0


def test_aggregate_handles_zero_samples() -> None:
    log = _FakeEvalLog(samples=[], scores=[])
    metrics = _aggregate_metrics(inspect_results=log, sample_count=0)
    assert metrics["sample_count"] == 0
    assert metrics["cost_usd_total"] is None
    assert metrics["cost_usd_coverage"] == 0.0
    assert metrics["latency_ms"]["p50"] is None


def test_aggregate_multi_reducer_keys_do_not_collide() -> None:
    """With epochs > 1 the same scorer appears once per reducer. Keying
    by bare name would let the second entry silently overwrite the
    first — the exact bug this reducer-qualified keying prevents."""
    log = _FakeEvalLog(
        samples=[],
        scores=[
            _FakeScore("answer_check", accuracy=0.8, reducer="mean"),
            _FakeScore(
                "answer_check", accuracy=0.4, reducer="at_least_5",
            ),
        ],
    )
    metrics = _aggregate_metrics(
        inspect_results=log, sample_count=10, epochs=5,
    )
    scorers = metrics["scorers"]
    assert scorers["answer_check/mean"]["accuracy"] == 0.8
    assert scorers["answer_check/at_least_5"]["accuracy"] == 0.4
    assert metrics["epochs"] == 5
    assert metrics["trial_count"] == 50


def test_aggregate_single_epoch_keeps_bare_scorer_names() -> None:
    """reducer=None (single-epoch logs) must keep the bare name so
    existing --threshold specs stay valid."""
    log = _FakeEvalLog(
        samples=[],
        scores=[_FakeScore("answer_check", accuracy=0.9)],
    )
    metrics = _aggregate_metrics(inspect_results=log, sample_count=3)
    assert "answer_check" in metrics["scorers"]
    assert metrics["epochs"] == 1
    assert metrics["trial_count"] == 3


def test_aggregate_epochs_coverage_uses_trial_count() -> None:
    """With 2 samples x 2 epochs, 4 trials run; if all 4 report cost
    the coverage is 1.0 — a sample_count denominator would report an
    impossible 2.0."""
    samples = [
        _FakeSample(latency_ms=100, usage={"cost_usd": 0.01})
        for _ in range(4)
    ]
    log = _FakeEvalLog(samples=samples, scores=[])
    metrics = _aggregate_metrics(
        inspect_results=log, sample_count=2, epochs=2,
    )
    assert abs(metrics["cost_usd_coverage"] - 1.0) < 1e-6
    assert metrics["cost_usd_total"] == pytest.approx(0.04)


def test_aggregate_counts_noanswer_per_scorer() -> None:
    """NOANSWER marks grading-infrastructure failure; Inspect's
    accuracy folds it in as 0, so the count must be surfaced or an
    outage silently reads as agent regression."""
    samples = [
        _FakeSample(scores={
            "answer_check": _FakeSampleScore("C"),
            "state_check": _FakeSampleScore("N"),
        }),
        _FakeSample(scores={
            "answer_check": _FakeSampleScore("N"),
            "state_check": _FakeSampleScore("N"),
        }),
        _FakeSample(scores={
            "answer_check": _FakeSampleScore("I"),
            "state_check": _FakeSampleScore(0.5),
        }),
    ]
    log = _FakeEvalLog(samples=samples, scores=[])
    metrics = _aggregate_metrics(inspect_results=log, sample_count=3)
    assert metrics["noanswer"] == {"answer_check": 1, "state_check": 2}


def test_aggregate_noanswer_empty_when_all_graded() -> None:
    samples = [
        _FakeSample(scores={"answer_check": _FakeSampleScore("C")}),
    ]
    log = _FakeEvalLog(samples=samples, scores=[])
    metrics = _aggregate_metrics(inspect_results=log, sample_count=1)
    assert metrics["noanswer"] == {}


def test_threshold_with_reducer_qualified_key() -> None:
    """The '/' in reducer-qualified keys must survive the dotted-path
    lookup (split is on '.', not '/')."""
    metrics = {
        "scorers": {"answer_check/at_least_5": {"accuracy": 0.75}},
    }
    assert _check_thresholds(
        metrics, ["scorers.answer_check/at_least_5.accuracy>=0.7"],
    ) == []
    failures = _check_thresholds(
        metrics, ["scorers.answer_check/at_least_5.accuracy>=0.8"],
    )
    assert len(failures) == 1
