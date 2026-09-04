"""state_check scorer — probe execution, assertions, failure semantics.

Network is stubbed at the module's ``_http_get`` seam; no server. The
mutation-testing posture drives case selection: partial-credit
arithmetic, the evidence-vs-infrastructure split, and trial_id
substitution each get a boundary assertion.
"""

from __future__ import annotations

from typing import Any

import pytest

# The eval extra (inspect-ai) is nightly-only: PR CI runs without it.
pytest.importorskip("inspect_ai")

import importlib

from inspect_ai.model import ModelName, ModelOutput
from inspect_ai.scorer import NOANSWER, Target
from inspect_ai.solver import TaskState

# The package re-exports the state_check *function* under the same
# name as its module, so attribute access on the package yields the
# function; go through sys.modules for the real module (monkeypatch
# targets live there).
sc_mod = importlib.import_module("rolemesh.evaluation.scorers.state_check")
_check_assertion = sc_mod._check_assertion
_lookup = sc_mod._lookup
state_check = sc_mod.state_check


def _state(
    *, probes: list[dict[str, Any]], trial_id: str = "eval-r1-0-e1",
) -> TaskState:
    return TaskState(
        model=ModelName("test/test"),
        sample_id="s1",
        epoch=1,
        input="do the thing for {{trial_id}}",
        messages=[],
        metadata={
            "state_check": {"probes": probes},
            "trial_id": trial_id,
        },
        output=ModelOutput.from_content("test/test", "done"),
    )


def _stub_get(responses: dict[str, tuple[int, Any]], calls: list[str]):
    async def fake(url: str, headers: dict[str, str]) -> tuple[int, Any]:
        calls.append(url)
        if url not in responses:
            raise sc_mod._InfraError(f"GET {url} failed: unreachable")
        return responses[url]

    return fake


# ---------------------------------------------------------------------------
# Path lookup + assertion ops
# ---------------------------------------------------------------------------


def test_lookup_nested_and_list_index() -> None:
    body = {"items": [{"status": "open"}, {"status": "done"}]}
    assert _lookup(body, "items.1.status") == (True, "done")
    assert _lookup(body, "items.5.status") == (False, None)
    assert _lookup(body, "missing.path") == (False, None)


def test_assertion_ops() -> None:
    body = {"status": "resolved", "count": 3, "labels": ["a", "b"]}
    ok, _ = _check_assertion(
        body, {"path": "status", "op": "equals", "value": "resolved"}, "t",
    )
    assert ok
    ok, _ = _check_assertion(
        body, {"path": "labels", "op": "contains", "value": "b"}, "t",
    )
    assert ok
    ok, _ = _check_assertion(body, {"path": "count", "op": "gte", "value": 3}, "t")
    assert ok  # boundary: gte is inclusive
    ok, _ = _check_assertion(body, {"path": "count", "op": "gte", "value": 4}, "t")
    assert not ok
    ok, _ = _check_assertion(body, {"path": "status", "op": "exists"}, "t")
    assert ok
    ok, _ = _check_assertion(body, {"path": "gone", "op": "absent"}, "t")
    assert ok
    ok, _ = _check_assertion(
        body, {"path": "status", "op": "matches", "value": r"^res"}, "t",
    )
    assert ok


def test_assertion_trial_id_substituted_in_expected_value() -> None:
    body = {"owner": "eval-r1-0-e1"}
    ok, _ = _check_assertion(
        body,
        {"path": "owner", "op": "equals", "value": "{{trial_id}}"},
        "eval-r1-0-e1",
    )
    assert ok


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_pass_scores_one(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sc_mod, "_http_get", _stub_get(
        {"https://s/api/eval-r1-0-e1": (200, {"status": "done"})}, calls,
    ))
    score = await state_check()(_state(probes=[{
        "url": "https://s/api/{{trial_id}}",
        "expect_status": 200,
        "assertions": [{"path": "status", "op": "equals", "value": "done"}],
    }]), Target("t"))
    assert score.value == 1.0
    # trial_id must be substituted into the URL before fetching.
    assert calls == ["https://s/api/eval-r1-0-e1"]


@pytest.mark.asyncio
async def test_partial_credit_fraction(monkeypatch) -> None:
    """status check passes, one of two assertions fails → 2/3. A
    mutation to short-circuit or to binary scoring breaks this."""
    monkeypatch.setattr(sc_mod, "_http_get", _stub_get(
        {"https://s/x": (200, {"status": "open", "assignee": "bot"})}, [],
    ))
    score = await state_check()(_state(probes=[{
        "url": "https://s/x",
        "expect_status": 200,
        "assertions": [
            {"path": "status", "op": "equals", "value": "done"},
            {"path": "assignee", "op": "equals", "value": "bot"},
        ],
    }]), Target("t"))
    assert score.value == pytest.approx(2 / 3)
    assert "FAIL" in (score.explanation or "")


@pytest.mark.asyncio
async def test_wrong_status_is_evidence_not_infra(monkeypatch) -> None:
    """A 404 that arrives is a graded failure — the entity the agent
    claimed to create isn't there."""
    monkeypatch.setattr(sc_mod, "_http_get", _stub_get(
        {"https://s/x": (404, {"error": "not found"})}, [],
    ))
    score = await state_check()(_state(probes=[{
        "url": "https://s/x", "expect_status": 200, "assertions": [],
    }]), Target("t"))
    assert score.value == 0.0
    assert score.value != NOANSWER


@pytest.mark.asyncio
async def test_unreachable_staging_scores_noanswer(monkeypatch) -> None:
    """Connect failure is grading-infrastructure failure — NOANSWER,
    never INCORRECT. Also pins that the retry seam is exercised
    (fetch raises _InfraError after retries)."""
    monkeypatch.setattr(sc_mod, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(sc_mod, "_http_get", _stub_get({}, []))
    score = await state_check()(_state(probes=[{
        "url": "https://down.example/x", "assertions": [],
    }]), Target("t"))
    assert score.value == NOANSWER
    assert "staging unreachable" in (score.explanation or "")


@pytest.mark.asyncio
async def test_missing_spec_is_noanswer() -> None:
    """Scorer attached without a spec = glue breakage, not a grade."""
    state = _state(probes=[])
    score = await state_check()(state, Target("t"))
    assert score.value == NOANSWER


@pytest.mark.asyncio
async def test_missing_env_var_scores_noanswer(monkeypatch) -> None:
    monkeypatch.delenv("EVAL_GONE_TOKEN", raising=False)
    monkeypatch.setattr(sc_mod, "_http_get", _stub_get(
        {"https://s/x": (200, {})}, [],
    ))
    score = await state_check()(_state(probes=[{
        "url": "https://s/x",
        "headers": {"Authorization": "${EVAL_GONE_TOKEN}"},
        "assertions": [],
    }]), Target("t"))
    assert score.value == NOANSWER
