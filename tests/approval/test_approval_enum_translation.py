"""INV-7 pinned test — wire ↔ engine enum translation for approvals.

The three wire vocabularies (HTTP ``action``, WS inbound
``decision``, WS outbound ``decision``) and the engine
``ApprovalOutcome`` evolve independently. This file pins the
mapping so a single-side change immediately flips it red.

Anti-mirror posture: the test never imports the internal dicts —
it lists the expected (wire ↔ engine) pairs by hand. Re-deriving
from the implementation would let a renamed key drift silently;
the explicit pairs are the contract.
"""

from __future__ import annotations

import pytest

from rolemesh.approval.enum_translate import (
    http_action_to_outcome,
    outcome_to_ws_decision,
    ws_decision_to_outcome,
)


# ---------------------------------------------------------------------------
# HTTP action -> engine outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("approve", "approved"),
        ("reject", "rejected"),
    ],
)
def test_http_action_translates_to_engine_outcome(
    action: str, expected: str
) -> None:
    assert http_action_to_outcome(action) == expected


@pytest.mark.parametrize(
    "bad_value",
    [
        "deny",       # WS-shaped value — must NOT be accepted on HTTP
        "approved",   # engine-shaped value
        "yes",
        "Approve",    # case sensitivity
        "",
        " approve ",  # whitespace not stripped — caller's job
    ],
)
def test_http_action_rejects_illegal_value(bad_value: str) -> None:
    """Illegal values raise; never silent default to approve/reject.

    A silent fallback would mask SPA bugs where a button label
    drifted away from the wire enum without anyone noticing.
    """
    with pytest.raises(ValueError):
        http_action_to_outcome(bad_value)


# ---------------------------------------------------------------------------
# WS inbound decision -> engine outcome
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("approve", "approved"),
        ("deny", "rejected"),  # WS uses "deny" not "reject"
    ],
)
def test_ws_decision_translates_to_engine_outcome(
    decision: str, expected: str
) -> None:
    assert ws_decision_to_outcome(decision) == expected


@pytest.mark.parametrize(
    "bad_value",
    [
        "reject",     # HTTP-shaped — must NOT be accepted on WS in
        "rejected",   # engine-shaped
        "approved",
        "expired",
        "cancelled",
        "",
    ],
)
def test_ws_decision_rejects_illegal_value(bad_value: str) -> None:
    with pytest.raises(ValueError):
        ws_decision_to_outcome(bad_value)


# ---------------------------------------------------------------------------
# Engine outcome -> WS outbound decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("approved", "approve"),
        ("rejected", "deny"),
        ("expired", "expired"),
        ("cancelled", "cancelled"),
    ],
)
def test_engine_outcome_translates_to_ws_decision(
    outcome: str, expected: str
) -> None:
    assert outcome_to_ws_decision(outcome) == expected


@pytest.mark.parametrize(
    "bad_value",
    [
        "approve",   # wire-shaped on the engine side
        "reject",
        "deny",
        "skipped",   # engine status that is NOT an outcome
        "pending",
        "",
    ],
)
def test_engine_outcome_rejects_illegal_value(bad_value: str) -> None:
    with pytest.raises(ValueError):
        outcome_to_ws_decision(bad_value)


# ---------------------------------------------------------------------------
# Round-trip (TestResolvedDecisionMap)
# ---------------------------------------------------------------------------


class TestResolvedDecisionMap:
    """End-to-end round-trip for HTTP / WS inbound -> engine -> WS outbound.

    A change to any side that breaks the loop will fail here, which
    is the cheapest place to catch it.
    """

    @pytest.mark.parametrize(
        ("http_action", "ws_in_decision", "ws_out_decision"),
        [
            ("approve", "approve", "approve"),
            ("reject", "deny", "deny"),
        ],
    )
    def test_http_and_ws_in_resolve_to_same_engine_outcome_and_round_trip_via_ws_out(
        self,
        http_action: str,
        ws_in_decision: str,
        ws_out_decision: str,
    ) -> None:
        engine_from_http = http_action_to_outcome(http_action)
        engine_from_ws = ws_decision_to_outcome(ws_in_decision)
        assert engine_from_http == engine_from_ws, (
            "HTTP and WS inbound must resolve to the same engine "
            "outcome for matching button semantics"
        )
        # Round-trip back out the WS event stream
        assert outcome_to_ws_decision(engine_from_http) == ws_out_decision

    @pytest.mark.parametrize(
        "outcome_only_reachable_from_engine",
        ["expired", "cancelled"],
    )
    def test_engine_only_outcomes_have_no_wire_inbound(
        self, outcome_only_reachable_from_engine: str
    ) -> None:
        """``expired``/``cancelled`` come only from engine internals.

        Neither HTTP nor WS inbound can request them — the engine
        decides expiry on its own and cancellation flows through
        the run-cancel path. The WS outbound mapping must still
        accept them so they reach the UI.
        """
        # Outbound mapping must accept them
        assert outcome_to_ws_decision(
            outcome_only_reachable_from_engine
        ) == outcome_only_reachable_from_engine
        # Inbound HTTP / WS must NOT accept them
        with pytest.raises(ValueError):
            http_action_to_outcome(outcome_only_reachable_from_engine)
        with pytest.raises(ValueError):
            ws_decision_to_outcome(outcome_only_reachable_from_engine)
