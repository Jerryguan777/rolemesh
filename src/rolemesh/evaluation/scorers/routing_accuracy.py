"""routing_accuracy scorer — did the frontdesk delegate to the right
specialist (or correctly NOT delegate when none was applicable)?

Per handbook §6 Step 8.3 the scoring rules are:

  - Trace contains ``delegate_to_agent`` AND ``target`` matches
    ``expected_target`` -> 1.
  - Trace contains ``delegate_to_agent`` but ``target`` is wrong
    -> 0.
  - Trace contains no ``delegate_to_agent`` and ``expected_target``
    is set -> 0.
  - Trace contains no ``delegate_to_agent`` and ``expected_target``
    is null -> 1 (the frontdesk correctly answered itself, e.g. a
    greeting or meta question).

Multi-delegation handling — a frontdesk turn MAY call
``delegate_to_agent`` more than once (handbook decision #6 allows
parallel routing). For v1.2 the dataset's ``expected_target`` is a
single string, so the contract is: "at least one delegation must hit
the expected target, AND no delegation may hit an unrelated
specialist". The second clause guards against the LLM hedging by
broadcasting the same query to every specialist — that's
operationally expensive even when one of the targets is correct.

The scorer reads ``observed_tool_calls`` and the parallel
``observed_tool_inputs`` (where the ``delegate_to_agent`` slot
contains the ``target`` argument, populated by
agent_runner.backend.tool_input_preview's frontdesk-v1.2 branch).
The ``scoring.routing.expected_target`` field is forwarded into
``state.metadata`` by inspect_glue.

Samples WITHOUT ``scoring.routing`` are marked CORRECT — no
requirement = no failure mode. Same opt-in posture as
``tool_trace_scorer``: dataset writers add the spec only on samples
where they want a routing signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    accuracy,
    scorer,
    stderr,
)

if TYPE_CHECKING:
    from inspect_ai.scorer import Target
    from inspect_ai.solver import TaskState


_DELEGATE_TOOL_NAME = "delegate_to_agent"


def _normalize(name: str) -> str:
    """Match the tool_trace scorer's normalization so the same Pi /
    Claude SDK casing differences (``Bash`` vs ``bash``) don't trip
    the comparison. Tool names from MCP servers also carry a
    namespaced prefix ``mcp__rolemesh__delegate_to_agent``; the runner
    strips that into the bare ``delegate_to_agent`` before publishing,
    but we tolerate both shapes here for robustness.
    """
    bare = name.rsplit("__", 1)[-1] if "__" in name else name
    return bare.strip().lower()


def _routing_spec(state: TaskState) -> dict[str, Any] | None:
    scoring = state.metadata.get("scoring")
    if not isinstance(scoring, dict):
        return None
    routing = scoring.get("routing")
    return routing if isinstance(routing, dict) else None


def _observed_delegations(state: TaskState) -> list[str]:
    """Return the list of ``target`` values from every
    ``delegate_to_agent`` call in this sample's trace.

    Walks the parallel ``observed_tool_calls`` / ``observed_tool_inputs``
    lists, filters to delegate-to-agent slots, and returns the inputs
    (which the backend.tool_input_preview frontdesk branch populates
    verbatim from ``args["target"]``).
    """
    raw_names = state.metadata.get("observed_tool_calls") or []
    raw_inputs = state.metadata.get("observed_tool_inputs") or []
    if not isinstance(raw_names, list) or not isinstance(raw_inputs, list):
        return []
    targets: list[str] = []
    # zip(strict=False) — if the runner ever truncates the input list
    # (defensive measure for an exception mid-turn) we want to score on
    # whatever we have rather than crash; an empty input is treated as
    # an unknown target downstream, which is the safe failure mode.
    for name, inp in zip(raw_names, raw_inputs, strict=False):
        if not isinstance(name, str) or not isinstance(inp, str):
            continue
        if _normalize(name) == _DELEGATE_TOOL_NAME:
            targets.append(inp.strip())
    return targets


@scorer(metrics=[accuracy(), stderr()])
def routing_accuracy_scorer() -> Scorer:
    """Pass/fail on whether the frontdesk routed to the expected
    specialist.

    See module docstring for the contract.
    """

    async def score(state: TaskState, target: Target) -> Score:
        spec = _routing_spec(state)
        if spec is None:
            return Score(value=CORRECT, explanation="no routing spec")

        expected = spec.get("expected_target")
        if expected is not None and not isinstance(expected, str):
            return Score(
                value=INCORRECT,
                explanation=(
                    "scoring.routing.expected_target must be a string "
                    f"or null; got {type(expected).__name__}"
                ),
            )

        observed = _observed_delegations(state)

        # No-match case: the LLM correctly answered itself iff the
        # trace contains no delegate_to_agent call. A spurious
        # delegation here is a fail — the eval gates against routing
        # noise (e.g. delegating a "Hi!" to portfolio).
        if expected is None:
            if not observed:
                return Score(value=CORRECT, explanation="no-delegate; expected null")
            return Score(
                value=INCORRECT,
                answer=", ".join(observed),
                explanation=(
                    f"expected no delegation; observed delegate_to_agent "
                    f"calls with targets: {observed}"
                ),
            )

        if not observed:
            return Score(
                value=INCORRECT,
                explanation=(
                    f"expected delegate_to_agent(target={expected!r}); "
                    f"no delegate_to_agent call observed"
                ),
            )

        # Multi-delegate handling: at least one must hit the expected
        # target AND every observed target must equal it (no fan-out
        # to wrong specialists). The second clause catches the
        # "hedge-by-broadcast" failure mode where the LLM delegates to
        # all specialists and lets one of them happen to be right.
        wrong = [t for t in observed if t != expected]
        if wrong:
            return Score(
                value=INCORRECT,
                answer=", ".join(observed),
                explanation=(
                    f"expected target {expected!r}; observed "
                    f"wrong targets: {wrong}"
                ),
            )
        if expected not in observed:
            # Defensive: with the previous "no wrong" check passing
            # and observed non-empty, we should have hit the expected
            # target. This branch fires only if observed contains
            # entries that match neither expected nor anything else
            # (e.g. empty strings) — score INCORRECT with a clear
            # explanation so the operator can fix the dataset / runner.
            return Score(
                value=INCORRECT,
                answer=", ".join(observed),
                explanation=(
                    f"observed delegate_to_agent slots had no usable "
                    f"target; could not match expected {expected!r}"
                ),
            )
        return Score(value=CORRECT, answer=expected)

    return score
