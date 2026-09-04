"""Dataset loader tests — boundary conditions and adversarial inputs.

The load path is the only thing standing between operator typos and a
silently-skewed accuracy number, so failures must be loud. These
tests poke at duplicate ids, missing required fields, malformed
probes, the homogeneity rule, the credential/env discipline, the
{{trial_id}} solvability check, and the SHA-256 reproducibility
property.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from rolemesh.evaluation.dataset import load_dataset


def _write(tmp: Path, rows: list[dict]) -> Path:
    p = tmp / "data.jsonl"
    p.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )
    return p


def _ok_row(idx: int = 0) -> dict:
    return {
        "id": f"q{idx}",
        "input": "what is 2+2?",
        "target": "the reply states the answer is 4",
    }


def _probe(**overrides: Any) -> dict:
    probe = {
        "url": "https://staging.example/api/items/{{trial_id}}",
        "expect_status": 200,
        "assert": [{"path": "status", "op": "equals", "value": "done"}],
    }
    probe.update(overrides)
    return probe


def _state_row(idx: int = 0, **probe_overrides: Any) -> dict:
    return {
        "id": f"s{idx}",
        "input": "close the ticket for customer {{trial_id}}",
        "target": "the reply confirms the ticket is closed",
        "scoring": {"state_check": {"probes": [_probe(**probe_overrides)]}},
    }


# ---------------------------------------------------------------------------
# Basic shape
# ---------------------------------------------------------------------------


def test_loads_minimal_row(tmp_path: Path) -> None:
    p = _write(tmp_path, [_ok_row(0), _ok_row(1)])
    ds = load_dataset(p)
    assert len(ds.samples) == 2
    assert ds.samples[0].id == "q0"
    assert ds.samples[0].target[0].startswith("the reply")
    assert ds.samples[0].state_check is None
    assert not ds.has_state_check


def test_sha256_stable_across_loads(tmp_path: Path) -> None:
    p = _write(tmp_path, [_ok_row()])
    assert load_dataset(p).sha256 == load_dataset(p).sha256


def test_missing_id_rejected(tmp_path: Path) -> None:
    row = _ok_row()
    del row["id"]
    with pytest.raises(ValueError, match="'id'"):
        load_dataset(_write(tmp_path, [row]))


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(_write(tmp_path, [_ok_row(0), _ok_row(0)]))


def test_missing_target_rejected(tmp_path: Path) -> None:
    """target is the judge criterion — a sample without one is
    ungradable and must fail at load, not at scoring."""
    row = _ok_row()
    del row["target"]
    with pytest.raises(ValueError, match="target"):
        load_dataset(_write(tmp_path, [row]))


def test_blank_target_rejected(tmp_path: Path) -> None:
    row = _ok_row()
    row["target"] = "   "
    with pytest.raises(ValueError, match="target"):
        load_dataset(_write(tmp_path, [row]))


def test_string_target_normalized_to_list(tmp_path: Path) -> None:
    """Single-string rows keep working; downstream sees one shape."""
    ds = load_dataset(_write(tmp_path, [_ok_row()]))
    assert ds.samples[0].target == ["the reply states the answer is 4"]


def test_multi_rubric_target_list(tmp_path: Path) -> None:
    row = _ok_row()
    row["target"] = ["mentions the answer 4", "shows the working"]
    ds = load_dataset(_write(tmp_path, [row]))
    assert ds.samples[0].target == ["mentions the answer 4", "shows the working"]


def test_empty_target_list_rejected(tmp_path: Path) -> None:
    row = _ok_row()
    row["target"] = []
    with pytest.raises(ValueError, match="target"):
        load_dataset(_write(tmp_path, [row]))


def test_blank_rubric_in_target_list_rejected(tmp_path: Path) -> None:
    row = _ok_row()
    row["target"] = ["fine", "  "]
    with pytest.raises(ValueError, match="target"):
        load_dataset(_write(tmp_path, [row]))


def test_legacy_final_answer_key_rejected(tmp_path: Path) -> None:
    """Pre-migration datasets carried scoring.final_answer; silently
    ignoring it would let an author believe a mode is still graded."""
    row = _ok_row()
    row["scoring"] = {"final_answer": {"mode": "exact", "target": "4"}}
    with pytest.raises(ValueError, match="unsupported scoring key"):
        load_dataset(_write(tmp_path, [row]))


def test_empty_dataset_rejected(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    p.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_dataset(p)


def test_invalid_json_line_rejected(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    p.write_text('{"id": "q0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_dataset(p)


def test_blank_lines_skipped(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    rows = [json.dumps(_ok_row(0)), "", json.dumps(_ok_row(1)), ""]
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert len(load_dataset(p).samples) == 2


# ---------------------------------------------------------------------------
# state_check probes
# ---------------------------------------------------------------------------


def test_state_check_parses(tmp_path: Path) -> None:
    ds = load_dataset(_write(tmp_path, [_state_row()]))
    assert ds.has_state_check
    spec = ds.samples[0].state_check
    assert spec is not None
    probe = spec.probes[0]
    assert probe.expect_status == 200
    assert probe.assertions[0].op == "equals"
    assert probe.assertions[0].value == "done"


def test_probe_requires_http_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="http"):
        load_dataset(_write(tmp_path, [_state_row(url="ftp://x/y")]))


def test_probe_rejects_unknown_assert_op(tmp_path: Path) -> None:
    row = _state_row()
    row["scoring"]["state_check"]["probes"][0]["assert"] = [
        {"path": "status", "op": "fuzzy", "value": "x"},
    ]
    with pytest.raises(ValueError, match=r"assert\.op"):
        load_dataset(_write(tmp_path, [row]))


def test_probe_exists_op_takes_no_value(tmp_path: Path) -> None:
    row = _state_row()
    row["scoring"]["state_check"]["probes"][0]["assert"] = [
        {"path": "status", "op": "exists", "value": "x"},
    ]
    with pytest.raises(ValueError, match="takes no value"):
        load_dataset(_write(tmp_path, [row]))


def test_probe_equals_requires_value(tmp_path: Path) -> None:
    row = _state_row()
    row["scoring"]["state_check"]["probes"][0]["assert"] = [
        {"path": "status", "op": "equals"},
    ]
    with pytest.raises(ValueError, match="requires a value"):
        load_dataset(_write(tmp_path, [row]))


def test_probe_empty_probes_rejected(tmp_path: Path) -> None:
    row = _state_row()
    row["scoring"]["state_check"]["probes"] = []
    with pytest.raises(ValueError, match="non-empty"):
        load_dataset(_write(tmp_path, [row]))


# ---------------------------------------------------------------------------
# Credential / env discipline
# ---------------------------------------------------------------------------


def test_literal_secret_in_header_rejected(tmp_path: Path) -> None:
    """Datasets are git content — a token-looking literal must never
    land in one."""
    row = _state_row(
        headers={"Authorization": "Bearer sk-ant-abcdef0123456789ABCDEF"},
    )
    with pytest.raises(ValueError, match="credential"):
        load_dataset(_write(tmp_path, [row]))


def test_env_ref_header_accepted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EVAL_STAGING_TOKEN", "tok")
    row = _state_row(headers={"Authorization": "Bearer ${EVAL_STAGING_TOKEN}"})
    ds = load_dataset(_write(tmp_path, [row]))
    probe = ds.samples[0].state_check.probes[0]
    # Stored unresolved — the scorer substitutes at score time.
    assert probe.headers["Authorization"] == "Bearer ${EVAL_STAGING_TOKEN}"


def test_missing_env_ref_fails_at_load(tmp_path: Path, monkeypatch) -> None:
    """Fail before any container spawns — same posture as the
    user-mode MCP pre-flight."""
    monkeypatch.delenv("EVAL_NO_SUCH_TOKEN", raising=False)
    row = _state_row(headers={"Authorization": "${EVAL_NO_SUCH_TOKEN}"})
    with pytest.raises(ValueError, match="EVAL_NO_SUCH_TOKEN"):
        load_dataset(_write(tmp_path, [row]))


# ---------------------------------------------------------------------------
# {{trial_id}} solvability
# ---------------------------------------------------------------------------


def test_probe_trial_var_requires_input_trial_var(tmp_path: Path) -> None:
    """A probe scoped to {{trial_id}} with an input that never mentions
    it is unsolvable — the agent can't know which entity to touch."""
    row = _state_row()
    row["input"] = "close the ticket"  # no {{trial_id}}
    with pytest.raises(ValueError, match="unsolvable"):
        load_dataset(_write(tmp_path, [row]))


def test_input_trial_var_without_probe_var_ok(tmp_path: Path) -> None:
    """The reverse is harmless: uniquely-named entities with probes
    that look them up another way."""
    row = _state_row(url="https://staging.example/api/latest")
    ds = load_dataset(_write(tmp_path, [row]))
    assert ds.has_state_check


# ---------------------------------------------------------------------------
# Homogeneity
# ---------------------------------------------------------------------------


def test_mixed_dataset_rejected(tmp_path: Path) -> None:
    """State-check and non-state-check samples in one file make the
    state_check accuracy column uninterpretable — split them."""
    with pytest.raises(ValueError, match="mixes"):
        load_dataset(_write(tmp_path, [_ok_row(0), _state_row(1)]))


def test_all_state_check_dataset_ok(tmp_path: Path) -> None:
    ds = load_dataset(_write(tmp_path, [_state_row(0), _state_row(1)]))
    assert ds.has_state_check
    assert len(ds.samples) == 2
