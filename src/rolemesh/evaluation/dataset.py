"""JSONL dataset loader for the eval framework.

One sample per line. The loader is strict — duplicate ids, missing
required fields, malformed probes, or a dataset that mixes state-check
and non-state-check samples raise immediately. A noisy schema error is
preferable to a quietly-skipped sample producing a deceptively high
accuracy.

Row shape (outcome-only, two grading axes):

  id       unique sample id
  input    prompt for the agent; may contain {{trial_id}}
  target   judge rubric(s) — what a correct reply looks like. A string
           or a non-empty list of strings; normalized to a list. Each
           entry is judged independently by model_graded_qa and the
           answer_check score is the fraction satisfied.
  scoring.state_check
           optional environment-state acceptance spec (see below).
           Homogeneous per dataset: every sample has one, or none do.
  metadata free-form annotations (tags, provenance); never graded.

state_check spec:

  probes: [{url, headers?, expect_status?, assert?: [{path, op, value?}]}]

  * v1 probes are HTTP GET only — the scorer never mutates state.
  * header values may reference host env vars as ${VAR}; literal
    secret-looking strings are rejected (datasets are git content).
  * url / header / assertion string values may contain {{trial_id}},
    which the scorer substitutes with the per-trial isolation key. A
    probe that uses it requires the input to use it too — otherwise
    the agent cannot know which entity to operate on and the task is
    unsolvable noise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRIAL_VAR = "{{trial_id}}"

_ASSERT_OPS: tuple[str, ...] = (
    "equals", "contains", "exists", "absent", "gte", "matches",
)
_OPS_WITHOUT_VALUE: tuple[str, ...] = ("exists", "absent")

_ENV_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
# Best-effort guard against literal credentials committed into a
# dataset: after stripping ${VAR} references and the trial template
# var, any long contiguous token-looking run is suspicious.
_SECRETISH_RE = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")


@dataclass(frozen=True)
class Assertion:
    """One check against a probe's JSON response body."""

    path: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class Probe:
    """One read-only HTTP GET against the staging backend."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    expect_status: int = 200
    assertions: list[Assertion] = field(default_factory=list)


@dataclass(frozen=True)
class StateCheckSpec:
    """Environment-state acceptance criteria for one sample."""

    probes: list[Probe]


@dataclass(frozen=True)
class Sample:
    """One row of the dataset.

    ``target`` is always a list after loading — a single-string row is
    normalized to a one-element list, so downstream code has exactly
    one shape to handle.
    """

    id: str
    input: str
    target: list[str]
    state_check: StateCheckSpec | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Dataset:
    path: str
    sha256: str
    samples: list[Sample]

    @property
    def has_state_check(self) -> bool:
        """Homogeneity is validated at load, so the first sample
        speaks for the dataset."""
        return bool(self.samples) and self.samples[0].state_check is not None


def _check_header_value(value: str, sample_id: str) -> None:
    """Reject header values that look like committed credentials."""
    stripped = _ENV_REF_RE.sub("", value).replace(TRIAL_VAR, "")
    if _SECRETISH_RE.search(stripped):
        msg = (
            f"sample {sample_id!r}: probe header value looks like a "
            f"literal credential — reference a host env var as "
            f"${{VAR}} instead (datasets are git content)"
        )
        raise ValueError(msg)


def _check_env_refs(value: str, sample_id: str) -> None:
    """Fail at load time if a referenced env var is missing — before
    any container spawns, matching the user-mode MCP pre-flight."""
    for var in _ENV_REF_RE.findall(value):
        if var not in os.environ:
            msg = (
                f"sample {sample_id!r}: probe references ${{{var}}} but "
                f"it is not set in the environment"
            )
            raise ValueError(msg)


def _parse_assertion(raw: Any, sample_id: str) -> Assertion:
    if not isinstance(raw, dict):
        msg = f"sample {sample_id!r}: each assert entry must be a dict"
        raise ValueError(msg)
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        msg = f"sample {sample_id!r}: assert.path must be a non-empty string"
        raise ValueError(msg)
    op = raw.get("op")
    if op not in _ASSERT_OPS:
        msg = (
            f"sample {sample_id!r}: assert.op must be one of "
            f"{_ASSERT_OPS}, got {op!r}"
        )
        raise ValueError(msg)
    has_value = "value" in raw
    if op in _OPS_WITHOUT_VALUE:
        if has_value:
            msg = (
                f"sample {sample_id!r}: assert.op {op!r} takes no value"
            )
            raise ValueError(msg)
        return Assertion(path=path, op=op)
    if not has_value:
        msg = f"sample {sample_id!r}: assert.op {op!r} requires a value"
        raise ValueError(msg)
    value = raw["value"]
    if op == "matches" and not isinstance(value, str):
        msg = f"sample {sample_id!r}: assert.op 'matches' needs a string value"
        raise ValueError(msg)
    if op == "gte" and not isinstance(value, (int, float)):
        msg = f"sample {sample_id!r}: assert.op 'gte' needs a numeric value"
        raise ValueError(msg)
    return Assertion(path=path, op=op, value=value)


def _parse_probe(raw: Any, sample_id: str) -> Probe:
    if not isinstance(raw, dict):
        msg = f"sample {sample_id!r}: each probe must be a dict"
        raise ValueError(msg)
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        msg = (
            f"sample {sample_id!r}: probe.url must be an http(s) URL, "
            f"got {url!r}"
        )
        raise ValueError(msg)
    _check_env_refs(url, sample_id)

    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in headers_raw.items()
    ):
        msg = f"sample {sample_id!r}: probe.headers must be a dict[str, str]"
        raise ValueError(msg)
    for v in headers_raw.values():
        _check_header_value(v, sample_id)
        _check_env_refs(v, sample_id)

    expect_status = raw.get("expect_status", 200)
    if not isinstance(expect_status, int) or not 100 <= expect_status <= 599:
        msg = (
            f"sample {sample_id!r}: probe.expect_status must be an HTTP "
            f"status code, got {expect_status!r}"
        )
        raise ValueError(msg)

    assertions_raw = raw.get("assert", [])
    if not isinstance(assertions_raw, list):
        msg = f"sample {sample_id!r}: probe.assert must be a list"
        raise ValueError(msg)
    assertions = [_parse_assertion(a, sample_id) for a in assertions_raw]
    return Probe(
        url=url,
        headers=dict(headers_raw),
        expect_status=expect_status,
        assertions=assertions,
    )


def _parse_state_check(raw: Any, sample_id: str) -> StateCheckSpec:
    if not isinstance(raw, dict):
        msg = f"sample {sample_id!r}: scoring.state_check must be a dict"
        raise ValueError(msg)
    probes_raw = raw.get("probes")
    if not isinstance(probes_raw, list) or not probes_raw:
        msg = (
            f"sample {sample_id!r}: scoring.state_check.probes must be a "
            f"non-empty list"
        )
        raise ValueError(msg)
    return StateCheckSpec(
        probes=[_parse_probe(p, sample_id) for p in probes_raw],
    )


def _probe_uses_trial_var(spec: StateCheckSpec) -> bool:
    for probe in spec.probes:
        if TRIAL_VAR in probe.url:
            return True
        if any(TRIAL_VAR in v for v in probe.headers.values()):
            return True
        for a in probe.assertions:
            if isinstance(a.value, str) and TRIAL_VAR in a.value:
                return True
    return False


def _parse_sample(line_no: int, raw: dict[str, Any]) -> Sample:
    sample_id = raw.get("id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        msg = f"line {line_no}: sample missing required string field 'id'"
        raise ValueError(msg)
    inp = raw.get("input")
    if not isinstance(inp, str) or not inp.strip():
        msg = f"sample {sample_id!r}: 'input' must be a non-empty string"
        raise ValueError(msg)
    target_raw = raw.get("target")
    if isinstance(target_raw, str):
        target = [target_raw]
    elif isinstance(target_raw, list):
        target = target_raw
    else:
        target = []
    if not target or not all(
        isinstance(t, str) and t.strip() for t in target
    ):
        msg = (
            f"sample {sample_id!r}: 'target' (judge rubrics) must be a "
            f"non-empty string or a non-empty list of non-empty strings"
        )
        raise ValueError(msg)

    scoring = raw.get("scoring") or {}
    if not isinstance(scoring, dict):
        msg = f"sample {sample_id!r}: 'scoring' must be a dict if provided"
        raise ValueError(msg)
    unknown = sorted(set(scoring) - {"state_check"})
    if unknown:
        msg = (
            f"sample {sample_id!r}: unsupported scoring key(s) {unknown} — "
            f"grading axes are the judge criterion in 'target' and "
            f"optional 'scoring.state_check'"
        )
        raise ValueError(msg)
    state_check = (
        _parse_state_check(scoring["state_check"], sample_id)
        if "state_check" in scoring else None
    )

    if (
        state_check is not None
        and _probe_uses_trial_var(state_check)
        and TRIAL_VAR not in inp
    ):
        msg = (
            f"sample {sample_id!r}: probes reference {TRIAL_VAR} but "
            f"'input' does not — the agent cannot know which entity "
            f"to operate on, so the task is unsolvable"
        )
        raise ValueError(msg)

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        msg = f"sample {sample_id!r}: 'metadata' must be a dict if provided"
        raise ValueError(msg)
    return Sample(
        id=sample_id,
        input=inp,
        target=target,
        state_check=state_check,
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

    # Homogeneity: mixing state-check and non-state-check samples makes
    # the state_check accuracy column uninterpretable (the no-spec rows
    # would need a filler grade that inflates or deflates it). One
    # dataset, one set of grading axes.
    with_probes = sum(1 for s in samples if s.state_check is not None)
    if 0 < with_probes < len(samples):
        msg = (
            f"dataset {p} mixes state-check and non-state-check samples "
            f"({with_probes}/{len(samples)} have scoring.state_check) — "
            f"split them into separate datasets"
        )
        raise ValueError(msg)

    return Dataset(path=str(p.resolve()), sha256=hash_file(p), samples=samples)
