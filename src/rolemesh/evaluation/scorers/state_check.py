"""state_check — verify environment state against the staging backend.

The outcome grader for state-mutating datasets: after the agent's turn
completes (container already shut down), read the staging system the
agent's MCP tools were pointed at and assert on what is actually there
— not on what the agent claimed. Probes are declarative data from the
dataset row (see ``dataset.py``); this module is their interpreter.

Grading semantics:

* every probe and every assertion is evaluated — no short-circuit;
  attribution wants the full picture, not the first failure;
* ``value`` is the fraction of checks passed (a probe's expect_status
  counts as one check). Partial credit feeds the ``mean`` metric;
  the pass^k reducers binarize at value == 1.0, so a 0.8 is a fail
  for the consistency gate — by design;
* evidence vs infrastructure: an HTTP response that arrives is
  evidence (a 404 against expect_status 200 is a graded failure), but
  failure to reach staging at all (connect error, timeout — after one
  retry) is grading-infrastructure failure and scores NOANSWER, never
  INCORRECT. Misreading an eval outage as agent regression is the
  worst failure mode an eval can have.

Independence: probes run host-side, straight at the staging API — not
through the credential proxy, egress gateway, or the MCP server under
test. The machinery being evaluated must not be the machinery doing
the verifying. Probes are GET-only; the scorer never mutates state.

Trial isolation: ``{{trial_id}}`` in urls / headers / assertion values
is substituted with the per-trial key the solver stored in
``state.metadata`` (the same value rendered into the agent's prompt),
so each trial reads only the entities it created. ``${VAR}`` header
references resolve from the host environment at score time; the loader
already verified they exist at load time.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import TYPE_CHECKING, Any

import aiohttp
from inspect_ai.scorer import (
    NOANSWER,
    Score,
    Scorer,
    accuracy,
    scorer,
    stderr,
)

if TYPE_CHECKING:
    from inspect_ai.scorer import Target
    from inspect_ai.solver import TaskState

TRIAL_VAR = "{{trial_id}}"

_ENV_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

_REQUEST_TIMEOUT_S = 10.0
_RETRY_DELAY_S = 1.0
# One retry: covers transient network blips and short eventual-
# consistency lag on the staging backend without stalling the run.
_ATTEMPTS = 2


class _InfraError(Exception):
    """Staging unreachable — grading infrastructure, not evidence."""


def _render(text: str, trial_id: str) -> str:
    """Substitute {{trial_id}} and ${ENV_VAR} references."""
    text = text.replace(TRIAL_VAR, trial_id)

    def _env(match: re.Match[str]) -> str:
        var = match.group(1)
        value = os.environ.get(var)
        if value is None:
            # Loader checked presence at load time; hitting this means
            # the environment changed under a long run — infra failure.
            msg = f"env var {var} vanished between load and scoring"
            raise _InfraError(msg)
        return value

    return _ENV_REF_RE.sub(_env, text)


async def _http_get(
    url: str, headers: dict[str, str],
) -> tuple[int, Any]:
    """Fetch one probe. Returns (status, parsed-or-raw body).

    Module-level so tests can monkeypatch the network seam without a
    real server. Raises _InfraError when staging is unreachable.
    """
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url, headers=headers) as resp,
        ):
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except (ValueError, aiohttp.ClientError):
                body = await resp.text()
            return status, body
    except (TimeoutError, aiohttp.ClientError) as exc:
        raise _InfraError(f"GET {url} failed: {exc!r}") from exc


async def _fetch_with_retry(
    url: str, headers: dict[str, str],
) -> tuple[int, Any]:
    last: _InfraError | None = None
    for attempt in range(_ATTEMPTS):
        try:
            return await _http_get(url, headers)
        except _InfraError as exc:
            last = exc
            if attempt + 1 < _ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_S)
    assert last is not None
    raise last


def _lookup(body: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path against the response body.

    Integer segments index lists. Returns (found, value) — absence is
    a first-class outcome (the ``absent`` op asserts on it), not an
    error.
    """
    node = body
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, node


def _check_assertion(body: Any, a: dict[str, Any], trial_id: str) -> tuple[bool, str]:
    """Evaluate one assertion. Returns (passed, description)."""
    path = str(a.get("path", ""))
    op = str(a.get("op", ""))
    expected = a.get("value")
    if isinstance(expected, str):
        expected = expected.replace(TRIAL_VAR, trial_id)

    found, actual = _lookup(body, path)
    label = f"{path} {op}" + ("" if expected is None else f" {expected!r}")

    if op == "exists":
        return found, f"{label} (found={found})"
    if op == "absent":
        return not found, f"{label} (found={found})"
    if not found:
        return False, f"{label} — path not present in response"
    if op == "equals":
        return actual == expected, f"{label} (got {actual!r})"
    if op == "contains":
        try:
            ok = expected in actual
        except TypeError:
            ok = False
        return ok, f"{label} (got {actual!r})"
    if op == "gte":
        ok = isinstance(actual, (int, float)) and actual >= float(expected)
        return ok, f"{label} (got {actual!r})"
    if op == "matches":
        ok = isinstance(actual, str) and re.search(str(expected), actual) is not None
        return ok, f"{label} (got {actual!r})"
    # Loader validates ops; reaching here means spec corruption.
    return False, f"{label} — unknown op"


@scorer(metrics=[accuracy(), stderr()])
def state_check() -> Scorer:
    """Grade environment state via the sample's declarative probes."""

    async def score(state: TaskState, target: Target) -> Score:
        spec = state.metadata.get("state_check")
        if not isinstance(spec, dict) or not spec.get("probes"):
            # Homogeneous datasets mean this scorer is only attached
            # when every sample carries probes; missing spec is glue
            # breakage, not a gradable outcome.
            return Score(
                value=NOANSWER,
                explanation="no state_check spec in sample metadata",
            )
        trial_id = str(state.metadata.get("trial_id", ""))

        passed = 0
        total = 0
        lines: list[str] = []
        try:
            for idx, probe in enumerate(spec["probes"]):
                url = _render(str(probe.get("url", "")), trial_id)
                headers = {
                    k: _render(str(v), trial_id)
                    for k, v in (probe.get("headers") or {}).items()
                }
                status, body = await _fetch_with_retry(url, headers)

                expect_status = int(probe.get("expect_status", 200))
                total += 1
                status_ok = status == expect_status
                passed += int(status_ok)
                mark = "PASS" if status_ok else "FAIL"
                lines.append(
                    f"probe[{idx}] {mark} status {status} "
                    f"(expect {expect_status})",
                )
                for a in probe.get("assertions") or []:
                    total += 1
                    ok, desc = _check_assertion(body, a, trial_id)
                    passed += int(ok)
                    lines.append(
                        f"probe[{idx}] {'PASS' if ok else 'FAIL'} {desc}",
                    )
        except _InfraError as exc:
            # Partial evidence cannot be trusted as a fraction — the
            # unreachable probes might be the failing ones. Whole
            # sample becomes NOANSWER, with what did run for context.
            lines.append(f"INFRA {exc}")
            return Score(
                value=NOANSWER,
                explanation="staging unreachable — not graded\n"
                + "\n".join(lines),
            )

        return Score(
            value=(passed / total) if total else 0.0,
            explanation="\n".join(lines),
        )

    return score
