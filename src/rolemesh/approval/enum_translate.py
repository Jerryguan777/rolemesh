"""Wire ↔ engine enum translation for approval decisions (INV-7).

Three closed enums coexist in the system, deliberately distinct so
each transport can evolve without dragging the others along:

* **HTTP** ``POST /approvals/{id}/decide`` body — ``action: "approve" | "reject"``
* **WS** ``request.approval`` body — ``decision: "approve" | "deny"``
  (note the "deny" mismatch with HTTP's "reject" — the design pins
  the WS terminology to match what the SPA UI labels its button).
* **WS** ``event.approval.resolved`` body — ``decision: "approve" | "deny" | "expired" | "cancelled"``
* **Engine** internal ``ApprovalOutcome`` — ``"approved" | "rejected" | "expired" | "cancelled"``
  (matches the persisted ``approval_requests.status`` text).

This module is the *only* place where the four-way mapping is
written. Handlers translate at the wire boundary so engine code
never sees a wire-string literal, and engine results never escape
without being mapped to the active transport's enum. INV-7
(``TestResolvedDecisionMap``) pins the round-trip.

Why not a single bidirectional dict: the HTTP / WS / engine
vocabularies *intentionally* don't line up 1:1. ``"reject"`` and
``"deny"`` both exist (one per transport) and both map to engine
``"rejected"``; the reverse is ambiguous without knowing which
transport the result is heading to. Three named functions name
the direction explicitly so a careless reverse lookup can't
silently flip the wire shape.

Illegal values raise :class:`ValueError` — never silently fall
back to a default. A silent fallback would mask SPA bugs where a
new decision value was added on one side without updating the
other; the loud raise is the point of the translation layer.
"""

from __future__ import annotations

from typing import Literal

ApprovalOutcome = Literal["approved", "rejected", "expired", "cancelled"]

_HTTP_ACTIONS: dict[str, ApprovalOutcome] = {
    "approve": "approved",
    "reject": "rejected",
}

_WS_DECISIONS_IN: dict[str, ApprovalOutcome] = {
    "approve": "approved",
    "deny": "rejected",
}

_WS_DECISIONS_OUT: dict[ApprovalOutcome, str] = {
    "approved": "approve",
    "rejected": "deny",
    "expired": "expired",
    "cancelled": "cancelled",
}


def http_action_to_outcome(action: str) -> ApprovalOutcome:
    """Translate HTTP ``decide.action`` into the engine enum.

    Raises ``ValueError`` for any value not in the closed
    ``{"approve", "reject"}`` set. The handler should let this
    propagate as a 422 with the wire-enum violation in the
    payload — the user agent is sending nonsense and we want it
    fixed, not papered over.
    """
    try:
        return _HTTP_ACTIONS[action]
    except KeyError as exc:
        raise ValueError(
            f"Unknown HTTP decide.action {action!r}; "
            f"allowed: {sorted(_HTTP_ACTIONS)}"
        ) from exc


def ws_decision_to_outcome(decision: str) -> ApprovalOutcome:
    """Translate inbound WS ``request.approval.decision`` to engine enum.

    Note: WS uses ``"deny"`` whereas HTTP uses ``"reject"``. Both
    map to the same engine outcome ``"rejected"`` — keep the wire
    string as wide as the user-facing button label and let this
    boundary collapse the synonyms.
    """
    try:
        return _WS_DECISIONS_IN[decision]
    except KeyError as exc:
        raise ValueError(
            f"Unknown WS request.approval.decision {decision!r}; "
            f"allowed: {sorted(_WS_DECISIONS_IN)}"
        ) from exc


def outcome_to_ws_decision(outcome: str) -> str:
    """Translate engine outcome back to the WS ``event.approval.resolved`` enum.

    Accepts ``str`` rather than the ``ApprovalOutcome`` Literal at
    the runtime boundary because the engine surfaces outcomes as
    plain ``str`` from DB rows; the type-narrowing happens inside
    via dict lookup and the raise keeps illegal values out.
    """
    try:
        return _WS_DECISIONS_OUT[outcome]  # type: ignore[index]
    except KeyError as exc:
        raise ValueError(
            f"Unknown engine ApprovalOutcome {outcome!r}; "
            f"allowed: {sorted(_WS_DECISIONS_OUT)}"
        ) from exc


__all__ = [
    "ApprovalOutcome",
    "http_action_to_outcome",
    "outcome_to_ws_decision",
    "ws_decision_to_outcome",
]
