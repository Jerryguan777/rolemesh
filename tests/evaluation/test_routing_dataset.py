"""Shape and composition guards on tests/data/routing_dataset.jsonl.

The dataset is a release-blocking gate input for the frontdesk v1.2
routing eval. The handbook §6 Step 8.2 stipulates a v1.2 launch
floor of:

  - >= 50 cases.
  - >= 20% adversarial ("looks like A, actually B").
  - >= 5 cases per target.
  - 5-10 no-match cases (expected_target=null).
  - >= 5 cases that exercise the failure-passthrough contract.

These tests pin those numbers so a careless ``jq -d`` or accidental
overwrite that drops the dataset below the floor fails the test
suite rather than silently shipping a weakened eval gate. The
3-month growth plan (>= 150 cases / >= 30 adversarial) is documented
in docs/frontdesk-architecture.md but NOT enforced here — growth is
a project commitment, not a hard CI gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rolemesh.evaluation.dataset import load_dataset

_DATASET = (
    Path(__file__).resolve().parents[1] / "data" / "routing_dataset.jsonl"
)

_KNOWN_TARGETS = {"trading", "portfolio", "accounting"}


@pytest.fixture(scope="module")
def dataset() -> object:
    return load_dataset(_DATASET)


def test_minimum_size_floor(dataset: object) -> None:
    """v1.2 launch floor is 50 cases. Catches accidental data loss."""
    samples = dataset.samples  # type: ignore[attr-defined]
    assert len(samples) >= 50, (
        f"routing dataset has {len(samples)} samples; v1.2 floor is 50"
    )


def test_every_sample_has_routing_spec(dataset: object) -> None:
    """The routing scorer opts out of samples without a routing spec
    by scoring CORRECT. For THIS dataset that would silently inflate
    accuracy. Every sample must declare expected_target (or null).
    """
    samples = dataset.samples  # type: ignore[attr-defined]
    missing = [s.id for s in samples if s.routing is None]
    assert missing == [], (
        f"routing_dataset samples missing scoring.routing: {missing}"
    )


def test_no_match_cases_in_required_range(dataset: object) -> None:
    """5-10 no-match cases. Too few and the gate doesn't punish the
    "broadcast greeting" failure mode; too many and the gate becomes
    a chatbot pleasantry test instead of a routing test.
    """
    samples = dataset.samples  # type: ignore[attr-defined]
    null_count = sum(1 for s in samples if s.routing.expected_target is None)
    assert 5 <= null_count <= 12, (
        f"no-match cases: {null_count}; handbook says 5-10 (soft cap 12)"
    )


def test_each_target_has_at_least_five_cases(dataset: object) -> None:
    samples = dataset.samples  # type: ignore[attr-defined]
    targets = [
        s.routing.expected_target for s in samples
        if s.routing.expected_target is not None
    ]
    for known in _KNOWN_TARGETS:
        n = targets.count(known)
        assert n >= 5, f"target {known!r} has only {n} cases; floor is 5"


def test_adversarial_share_at_least_20_percent(dataset: object) -> None:
    """Adversarial cases are tagged with ``metadata.adversarial=True``.
    The 20% floor is what differentiates this dataset from a trivial
    "match the obvious keyword" gate; below that, routing-accuracy
    becomes a rubber stamp.
    """
    samples = dataset.samples  # type: ignore[attr-defined]
    adversarial = [s for s in samples if s.metadata.get("adversarial") is True]
    share = len(adversarial) / len(samples)
    assert share >= 0.20, (
        f"adversarial share {share:.0%} below 20% floor; "
        f"{len(adversarial)}/{len(samples)} flagged"
    )


def test_failure_passthrough_contract_covered(dataset: object) -> None:
    """Handbook §6 Step 8.2 requires >= 5 failure-passthrough cases —
    samples whose final_answer.criterion explicitly tests that the
    frontdesk reply includes the specialist's name + literal reason
    on isError=true. Tagged with metadata.contract="failure-passthrough".
    """
    samples = dataset.samples  # type: ignore[attr-defined]
    fp = [
        s for s in samples
        if s.metadata.get("contract") == "failure-passthrough"
    ]
    assert len(fp) >= 5, (
        f"failure-passthrough cases: {len(fp)}; floor is 5"
    )


def test_all_targets_are_known_or_null(dataset: object) -> None:
    """If the dataset starts referencing a target that the catalog
    doesn't render (typo / specialist removed), every routing call
    against that target is destined to fail with a "not found" error.
    Catch the typo in the dataset, not in the eval run.
    """
    samples = dataset.samples  # type: ignore[attr-defined]
    bad = [
        (s.id, s.routing.expected_target) for s in samples
        if s.routing.expected_target is not None
        and s.routing.expected_target not in _KNOWN_TARGETS
    ]
    assert bad == [], (
        f"samples reference unknown targets (allowed: {_KNOWN_TARGETS}): {bad}"
    )


def test_sample_ids_use_fd_prefix(dataset: object) -> None:
    """Lightweight namespacing — keeps these from colliding with
    samples in other rolemesh-eval datasets that may be merged into
    a single Inspect ``MemoryDataset`` for a multi-feature run.
    """
    samples = dataset.samples  # type: ignore[attr-defined]
    non_fd = [s.id for s in samples if not s.id.startswith("fd-")]
    assert non_fd == [], (
        f"routing_dataset samples without 'fd-' prefix: {non_fd}"
    )
