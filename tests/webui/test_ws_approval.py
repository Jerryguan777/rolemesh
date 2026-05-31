"""v1 WS HITL approval frames (docs/21-hitl-approval-plan.md §10 S4).

Covers the new client decision frame and server card events at the schema
level, the field-whitelisting projection from the orchestrator carrier, and the
``request.approval_decision`` relay — asserting the IDOR-critical property that
the approver identity is stamped from the verified ticket, never the frame.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest
from starlette.websockets import WebSocketState

from rolemesh.auth.ws_ticket import WsTicketPayload
from webui.schemas_v1 import (
    WsClientFrameApprovalDecision,
    WsClientFrameModel,
    WsServerEventApprovalRequested,
    WsServerEventApprovalResolved,
    WsServerEventModel,
)
from webui.v1.ws_stream import _build_approval_frame_or_none, _handle_approval_decision

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_client_decision_frame_validates() -> None:
    adapter = pydantic.TypeAdapter(WsClientFrameModel)
    frame = adapter.validate_python(
        {"type": "request.approval_decision", "request_id": "r1", "decision": "approve"}
    )
    assert isinstance(frame, WsClientFrameApprovalDecision)
    assert frame.decision == "approve"
    assert frame.note is None


def test_client_decision_frame_rejects_bad_decision() -> None:
    with pytest.raises(pydantic.ValidationError):
        WsClientFrameApprovalDecision.model_validate(
            {"type": "request.approval_decision", "request_id": "r1", "decision": "maybe"}
        )


def test_client_decision_frame_forbids_extra_identity_fields() -> None:
    # A browser must not be able to smuggle a decided_by/user_id into the frame.
    with pytest.raises(pydantic.ValidationError):
        WsClientFrameApprovalDecision.model_validate(
            {
                "type": "request.approval_decision",
                "request_id": "r1",
                "decision": "approve",
                "decided_by": "someone-else",
            }
        )


def test_server_approval_events_validate() -> None:
    adapter = pydantic.TypeAdapter(WsServerEventModel)
    requested = adapter.validate_python(
        {"type": "event.approval.requested", "request_id": "r1",
         "action_summary": "stripe.charge", "expires_at": "2026-05-31T00:00:00Z"}
    )
    assert isinstance(requested, WsServerEventApprovalRequested)
    resolved = adapter.validate_python(
        {"type": "event.approval.resolved", "request_id": "r1", "outcome": "rejected"}
    )
    assert isinstance(resolved, WsServerEventApprovalResolved)


def test_server_resolved_rejects_bad_outcome() -> None:
    with pytest.raises(pydantic.ValidationError):
        WsServerEventApprovalResolved.model_validate(
            {"type": "event.approval.resolved", "request_id": "r1", "outcome": "maybe"}
        )


# ---------------------------------------------------------------------------
# carrier → browser-frame projection (field whitelisting)
# ---------------------------------------------------------------------------


def test_build_requested_frame_whitelists_fields() -> None:
    frame = _build_approval_frame_or_none(
        {
            "type": "approval.requested",
            "request_id": "r1",
            "action_summary": "stripe.charge",
            "expires_at": "2026-05-31T00:00:00Z",
            "secret_internal_key": "must-not-leak",
        }
    )
    assert frame == {
        "type": "event.approval.requested",
        "request_id": "r1",
        "action_summary": "stripe.charge",
        "expires_at": "2026-05-31T00:00:00Z",
    }
    assert "secret_internal_key" not in frame


def test_build_resolved_frame() -> None:
    frame = _build_approval_frame_or_none(
        {"type": "approval.resolved", "request_id": "r1", "outcome": "approved"}
    )
    assert frame == {
        "type": "event.approval.resolved",
        "request_id": "r1",
        "outcome": "approved",
    }


def test_build_frame_drops_unknown_and_malformed() -> None:
    assert _build_approval_frame_or_none({"type": "approval.requested"}) is None  # no id
    assert _build_approval_frame_or_none({"type": "other", "request_id": "r1"}) is None
    assert (
        _build_approval_frame_or_none(
            {"type": "approval.resolved", "request_id": "r1", "outcome": "weird"}
        )
        is None
    )


# ---------------------------------------------------------------------------
# decision relay (IDOR: identity stamped from the ticket, not the frame)
# ---------------------------------------------------------------------------


class _FakeWs:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)


class _FakeJs:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))


def _payload() -> WsTicketPayload:
    return WsTicketPayload(
        user_id="ticket-user", tenant_id="t1", conversation_id="conv1", exp=9999999999
    )


def _conv() -> Any:
    return SimpleNamespace(id="conv1", channel_chat_id="chat1")


async def test_decision_relays_with_ticket_stamped_identity() -> None:
    ws, js = _FakeWs(), _FakeJs()
    await _handle_approval_decision(
        ws=ws,
        frame={"type": "request.approval_decision", "request_id": "r1",
               "decision": "approve", "decided_by": "attacker-supplied"},
        payload=_payload(),
        conv=_conv(),
        binding_id="bind1",
        js=js,
    )
    assert len(js.published) == 1
    subject, raw = js.published[0]
    assert subject == "web.approval_decision.bind1.chat1"
    body = json.loads(raw)
    assert body["request_id"] == "r1"
    assert body["decision"] == "approve"
    # decided_by/tenant_id come from the VERIFIED ticket, overriding whatever
    # the browser put in the frame — the IDOR guard.
    assert body["decided_by"] == "ticket-user"
    assert body["tenant_id"] == "t1"
    assert body["conversation_id"] == "conv1"


async def test_decision_missing_request_id_errors_no_publish() -> None:
    ws, js = _FakeWs(), _FakeJs()
    await _handle_approval_decision(
        ws=ws,
        frame={"type": "request.approval_decision", "decision": "approve"},
        payload=_payload(),
        conv=_conv(),
        binding_id="bind1",
        js=js,
    )
    assert js.published == []
    assert ws.sent and ws.sent[0]["code"] == "PROTOCOL_MISSING_REQUEST_ID"


async def test_decision_bad_verb_errors_no_publish() -> None:
    ws, js = _FakeWs(), _FakeJs()
    await _handle_approval_decision(
        ws=ws,
        frame={"type": "request.approval_decision", "request_id": "r1",
               "decision": "maybe"},
        payload=_payload(),
        conv=_conv(),
        binding_id="bind1",
        js=js,
    )
    assert js.published == []
    assert ws.sent and ws.sent[0]["code"] == "PROTOCOL_BAD_DECISION"
