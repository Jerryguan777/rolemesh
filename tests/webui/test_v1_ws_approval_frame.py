"""v1 WS HITL approval — real-transport integration (docs §10 S4).

Drives the actual ``ws_stream.stream`` receive loop + the new
``web.approval.*`` subscription through a FastAPI ``TestClient``, mocking only
the outermost boundaries (JetStream + ``get_conversation``). Complements the
direct-call unit tests in ``test_ws_approval.py`` by proving the frame routes
through the live dispatch and the forward task projects pushed events to the
browser.

No ``test_db`` fixture: the handshake's only DB touch is ``get_conversation``,
which we monkeypatch, so no Postgres is needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webui.v1 import ws_stream

_TEST_SECRET = "v1-ws-handshake-secret-only-for-tests"
os.environ.setdefault("WS_TICKET_SECRET", _TEST_SECRET)


class _Sub:
    """A subscription whose ``messages`` async-iter yields a queued backlog
    then blocks (mirrors a live ordered consumer)."""

    def __init__(self, backlog: list[bytes] | None = None) -> None:
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        for b in backlog or []:
            self._q.put_nowait(b)

    @property
    def messages(self):  # type: ignore[no-untyped-def]
        return self

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __anext__(self):  # type: ignore[no-untyped-def]
        data = await self._q.get()
        return _Msg(data)

    async def unsubscribe(self) -> None:
        return None


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def ack(self) -> None:
        return None


class _JS:
    """Captures publishes and hands out per-subject subscriptions.

    ``approval_backlog`` is delivered on the ``web.approval.*`` subscription so
    the test can assert the forward task projects it to the browser.
    """

    def __init__(self, approval_backlog: list[bytes] | None = None) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._approval_backlog = approval_backlog or []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def subscribe(self, subject: str, **_kw: Any) -> _Sub:
        if subject.startswith("web.approval."):
            return _Sub(self._approval_backlog)
        return _Sub()


class _Conv:
    def __init__(self, conv_id: str, tenant_id: str, binding_id: str, chat_id: str) -> None:
        self.id = conv_id
        self.tenant_id = tenant_id
        self.channel_binding_id = binding_id
        self.channel_chat_id = chat_id


def _mint(*, tenant_id: str, conv_id: str, user_id: str) -> str:
    payload = {
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(seconds=60)).timestamp()),
        "aud": "rolemesh-ws",
        "sub": user_id,
        "tenant_id": tenant_id,
        "conversation_id": conv_id,
    }
    return jwt.encode(payload, _TEST_SECRET, algorithm="HS256")


def _app() -> FastAPI:
    app = FastAPI()
    ws_stream.register_routes(app)
    return app


@pytest.fixture
def conv_stub(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    holder: dict[str, _Conv] = {}

    async def _fake(conversation_id: str, *, tenant_id: str) -> _Conv | None:
        return holder.get("conv")

    monkeypatch.setattr(ws_stream, "get_conversation", _fake)
    return holder


def test_approval_decision_frame_publishes_with_ticket_identity(
    conv_stub: dict[str, _Conv]
) -> None:
    conv_id, tenant_id = str(uuid.uuid4()), str(uuid.uuid4())
    binding_id, chat_id, user_id = str(uuid.uuid4()), "chat-apr", str(uuid.uuid4())
    conv_stub["conv"] = _Conv(conv_id, tenant_id, binding_id, chat_id)
    js = _JS()
    ws_stream.set_jetstream(js)  # type: ignore[arg-type]
    try:
        ticket = _mint(tenant_id=tenant_id, conv_id=conv_id, user_id=user_id)
        with TestClient(_app()) as client, client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
        ) as ws:
            ws.send_json({
                "type": "request.approval_decision",
                "request_id": "req-xyz",
                "decision": "approve",
                # Hostile identity override — MUST be ignored.
                "decided_by": "ATTACKER",
            })
            ws.send_json({"type": "request.unknown_probe"})
            err = ws.receive_json()
            assert err.get("code") == "PROTOCOL_UNKNOWN_TYPE"
    finally:
        ws_stream.set_jetstream(None)

    subject = f"web.approval_decision.{binding_id}.{chat_id}"
    relays = [p for p in js.published if p[0] == subject]
    assert len(relays) == 1
    body = json.loads(relays[0][1])
    assert body["request_id"] == "req-xyz"
    assert body["decision"] == "approve"
    # IDOR: decided_by/tenant_id stamped from the verified ticket, not payload.
    assert body["decided_by"] == user_id
    assert body["decided_by"] != "ATTACKER"
    assert body["tenant_id"] == tenant_id
    assert body["conversation_id"] == conv_id


def test_pushed_approval_event_is_forwarded_to_browser(
    conv_stub: dict[str, _Conv]
) -> None:
    conv_id, tenant_id = str(uuid.uuid4()), str(uuid.uuid4())
    binding_id, chat_id, user_id = str(uuid.uuid4()), "chat-fwd", str(uuid.uuid4())
    conv_stub["conv"] = _Conv(conv_id, tenant_id, binding_id, chat_id)
    # The orchestrator pushes a requested card onto web.approval.{binding}.{chat}.
    pushed = json.dumps({
        "type": "approval.requested",
        "request_id": "req-1",
        "action_summary": "stripe.charge",
        "expires_at": "2026-05-31T00:00:00Z",
        "internal_only": "must-not-leak",
    }).encode()
    js = _JS(approval_backlog=[pushed])
    ws_stream.set_jetstream(js)  # type: ignore[arg-type]
    try:
        ticket = _mint(tenant_id=tenant_id, conv_id=conv_id, user_id=user_id)
        with TestClient(_app()) as client, client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
        ) as ws:
            frame = ws.receive_json()
            assert frame == {
                "type": "event.approval.requested",
                "request_id": "req-1",
                "action_summary": "stripe.charge",
                "expires_at": "2026-05-31T00:00:00Z",
            }
            assert "internal_only" not in frame
    finally:
        ws_stream.set_jetstream(None)
