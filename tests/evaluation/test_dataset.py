"""Dataset loader tests — boundary conditions and adversarial inputs.

The load path is the only thing standing between operator typos and a
silently-skewed accuracy number, so failures must be loud. These
tests poke at duplicate ids, missing required fields, malformed mode
choices, and the SHA-256 reproducibility property.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from rolemesh.evaluation.dataset import load_dataset


def _write(tmp: Path, rows: list[dict]) -> Path:
    p = tmp / "data.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _ok_row(idx: int = 0) -> dict:
    return {
        "id": f"q{idx}",
        "input": "what is 2+2?",
        "scoring": {"final_answer": {"mode": "exact", "target": "4"}},
    }


def test_loads_minimal_jsonl(tmp_path: Path) -> None:
    p = _write(tmp_path, [_ok_row(0), _ok_row(1)])
    ds = load_dataset(p)
    assert len(ds.samples) == 2
    assert ds.samples[0].id == "q0"
    assert ds.samples[0].final_answer.mode == "exact"
    assert ds.samples[0].final_answer.target == "4"
    assert ds.sha256  # non-empty, populated


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    """Duplicate ids would conflate two samples into one Inspect run,
    silently halving the eval. Must be loud."""
    p = _write(tmp_path, [_ok_row(0), _ok_row(0)])
    with pytest.raises(ValueError, match="duplicate sample id"):
        load_dataset(p)


def test_rejects_missing_id(tmp_path: Path) -> None:
    bad = {"input": "x", "scoring": {"final_answer": {"mode": "exact", "target": ""}}}
    p = _write(tmp_path, [bad])
    with pytest.raises(ValueError, match="'id'"):
        load_dataset(p)


def test_rejects_blank_id(tmp_path: Path) -> None:
    """Empty-string id would pass a naive truthiness check downstream
    but is functionally indistinguishable from no id."""
    bad = {"id": "   ", "input": "x",
           "scoring": {"final_answer": {"mode": "exact", "target": ""}}}
    p = _write(tmp_path, [bad])
    with pytest.raises(ValueError, match="'id'"):
        load_dataset(p)


def test_rejects_unknown_mode(tmp_path: Path) -> None:
    bad = {"id": "q0", "input": "x",
           "scoring": {"final_answer": {"mode": "fuzzy"}}}
    p = _write(tmp_path, [bad])
    with pytest.raises(ValueError, match="mode"):
        load_dataset(p)


def test_exact_mode_requires_target(tmp_path: Path) -> None:
    bad = {"id": "q0", "input": "x",
           "scoring": {"final_answer": {"mode": "exact"}}}
    p = _write(tmp_path, [bad])
    with pytest.raises(ValueError, match="target"):
        load_dataset(p)


def test_regex_mode_requires_pattern(tmp_path: Path) -> None:
    bad = {"id": "q0", "input": "x",
           "scoring": {"final_answer": {"mode": "regex"}}}
    p = _write(tmp_path, [bad])
    with pytest.raises(ValueError, match="pattern"):
        load_dataset(p)


def test_judge_mode_requires_non_empty_criterion(tmp_path: Path) -> None:
    """Whitespace-only criterion is still empty in any meaningful sense."""
    bad = {"id": "q0", "input": "x",
           "scoring": {"final_answer": {"mode": "llm_judge", "criterion": "   "}}}
    p = _write(tmp_path, [bad])
    with pytest.raises(ValueError, match="criterion"):
        load_dataset(p)


def test_tool_trace_optional_and_validates(tmp_path: Path) -> None:
    row = _ok_row(0)
    row["scoring"]["tool_trace"] = {
        "required_tools": ["bash"],
        "forbidden_tools": ["rm"],
        "expected_order": ["read", "edit"],
    }
    p = _write(tmp_path, [row])
    ds = load_dataset(p)
    assert ds.samples[0].tool_trace is not None
    assert ds.samples[0].tool_trace.required_tools == ["bash"]
    assert ds.samples[0].tool_trace.expected_order == ["read", "edit"]


def test_tool_trace_rejects_non_string_list(tmp_path: Path) -> None:
    """Type confusion (numbers in a tool name list) silently mangles
    LCS output later — fail at load time."""
    row = _ok_row(0)
    row["scoring"]["tool_trace"] = {"required_tools": ["bash", 42]}
    p = _write(tmp_path, [row])
    with pytest.raises(ValueError, match="tool_trace"):
        load_dataset(p)


def test_blank_lines_skipped(tmp_path: Path) -> None:
    """A trailing newline or accidental blank in the middle of the
    file should not register as an empty sample."""
    p = tmp_path / "data.jsonl"
    p.write_text(
        json.dumps(_ok_row(0)) + "\n\n" + json.dumps(_ok_row(1)) + "\n",
        encoding="utf-8",
    )
    ds = load_dataset(p)
    assert len(ds.samples) == 2


def test_empty_file_rejected(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_dataset(p)


def test_malformed_json_loud(tmp_path: Path) -> None:
    p = tmp_path / "data.jsonl"
    p.write_text('{"id": "q0", "input": "x",\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_dataset(p)


def test_sha256_deterministic_and_distinguishes_changes(tmp_path: Path) -> None:
    """The same bytes must hash identically; a single byte change
    must produce a different hash so config-clustering doesn't
    confuse two genuinely different datasets."""
    p1 = _write(tmp_path, [_ok_row(0)])
    h1 = load_dataset(p1).sha256
    h2 = load_dataset(p1).sha256
    assert h1 == h2

    p2 = tmp_path / "data2.jsonl"
    p2.write_text(p1.read_text() + " ", encoding="utf-8")
    assert load_dataset(p2).sha256 != h1


def test_missing_file_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_dataset(tmp_path / "nope.jsonl")


def test_metadata_passthrough(tmp_path: Path) -> None:
    row = _ok_row(0)
    row["metadata"] = {"category": "math", "difficulty": "easy"}
    p = _write(tmp_path, [row])
    ds = load_dataset(p)
    assert ds.samples[0].metadata == {"category": "math", "difficulty": "easy"}
