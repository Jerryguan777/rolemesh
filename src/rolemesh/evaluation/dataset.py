"""JSONL dataset loader for the eval framework.

One sample per line. The loader is strict — duplicate ids, missing
required fields, or unknown ``final_answer.mode`` raise immediately.
A noisy schema error is preferable to a quietly-skipped sample
producing a deceptively high accuracy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ScoringMode = Literal["exact", "regex", "llm_judge"]
_VALID_MODES: tuple[str, ...] = ("exact", "regex", "llm_judge")


@dataclass(frozen=True)
class FinalAnswerSpec:
    """How to score a sample's final answer."""

    mode: ScoringMode
    target: str | None = None    # mode == "exact"
    pattern: str | None = None   # mode == "regex"
    criterion: str | None = None  # mode == "llm_judge"


@dataclass(frozen=True)
class Sample:
    """One row of the dataset."""

    id: str
    input: str
    final_answer: FinalAnswerSpec
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Dataset:
    path: str
    sha256: str
    samples: list[Sample]


def _parse_final_answer(raw: Any, sample_id: str) -> FinalAnswerSpec:
    if not isinstance(raw, dict):
        msg = f"sample {sample_id!r}: scoring.final_answer must be a dict"
        raise ValueError(msg)
    mode = raw.get("mode")
    if mode not in _VALID_MODES:
        msg = (
            f"sample {sample_id!r}: scoring.final_answer.mode must be one of "
            f"{_VALID_MODES}, got {mode!r}"
        )
        raise ValueError(msg)
    if mode == "exact":
        target = raw.get("target")
        if not isinstance(target, str):
            msg = (
                f"sample {sample_id!r}: scoring.final_answer.target must "
                f"be a string when mode='exact'"
            )
            raise ValueError(msg)
        return FinalAnswerSpec(mode="exact", target=target)
    if mode == "regex":
        pattern = raw.get("pattern")
        if not isinstance(pattern, str):
            msg = (
                f"sample {sample_id!r}: scoring.final_answer.pattern must "
                f"be a string when mode='regex'"
            )
            raise ValueError(msg)
        return FinalAnswerSpec(mode="regex", pattern=pattern)
    # llm_judge
    criterion = raw.get("criterion")
    if not isinstance(criterion, str) or not criterion.strip():
        msg = (
            f"sample {sample_id!r}: scoring.final_answer.criterion must "
            f"be a non-empty string when mode='llm_judge'"
        )
        raise ValueError(msg)
    return FinalAnswerSpec(mode="llm_judge", criterion=criterion)


def _parse_sample(line_no: int, raw: dict[str, Any]) -> Sample:
    sample_id = raw.get("id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        msg = f"line {line_no}: sample missing required string field 'id'"
        raise ValueError(msg)
    inp = raw.get("input")
    if not isinstance(inp, str):
        msg = f"sample {sample_id!r}: 'input' must be a string"
        raise ValueError(msg)
    scoring = raw.get("scoring")
    if not isinstance(scoring, dict):
        msg = f"sample {sample_id!r}: 'scoring' must be a dict"
        raise ValueError(msg)
    # Scoring is outcome-only. tool_trace / routing specs were removed
    # (eval/inspect-cleanup); rejecting the keys beats silently loading
    # a dataset whose author still expects them to be graded.
    unknown = sorted(set(scoring) - {"final_answer"})
    if unknown:
        msg = (
            f"sample {sample_id!r}: unsupported scoring key(s) {unknown} — "
            f"scoring is outcome-only ('final_answer'); tool_trace/routing "
            f"specs are no longer graded"
        )
        raise ValueError(msg)
    final_answer = _parse_final_answer(scoring.get("final_answer"), sample_id)
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        msg = f"sample {sample_id!r}: 'metadata' must be a dict if provided"
        raise ValueError(msg)
    return Sample(
        id=sample_id,
        input=inp,
        final_answer=final_answer,
        metadata=metadata,
    )


def hash_file(path: Path) -> str:
    """SHA-256 of the file's bytes — recorded with each run."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_dataset(path: str | Path) -> Dataset:
    """Load and validate a JSONL dataset file."""
    p = Path(path)
    if not p.is_file():
        msg = f"dataset file not found: {p}"
        raise FileNotFoundError(msg)

    samples: list[Sample] = []
    seen_ids: set[str] = set()
    with p.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                msg = f"line {line_no}: invalid JSON: {exc.msg}"
                raise ValueError(msg) from exc
            if not isinstance(obj, dict):
                msg = f"line {line_no}: top-level value must be an object"
                raise ValueError(msg)
            sample = _parse_sample(line_no, obj)
            if sample.id in seen_ids:
                msg = (
                    f"line {line_no}: duplicate sample id {sample.id!r} "
                    f"(every sample must have a unique id)"
                )
                raise ValueError(msg)
            seen_ids.add(sample.id)
            samples.append(sample)

    if not samples:
        msg = f"dataset {p} is empty"
        raise ValueError(msg)

    return Dataset(path=str(p.resolve()), sha256=hash_file(p), samples=samples)
