"""v1 WS ``request.stop`` frame publishes to ``web.stop.{binding}.{chat}``.

Background — pre-PR-B, the Stop button rode the legacy ``/ws/chat``
endpoint via ``AgentClient.stop()``, sending a free-form
``{type:"stop"}`` frame. PR-B moves that into the v1 protocol so
the SPA only needs one WebSocket per chat-panel, after which the
legacy endpoint can be deleted.

Key contract pinned here:

* The frame type is ``request.stop`` (matches the OpenAPI client-frame
  oneOf entry).
* Receipt → publish to the same ``web.stop.{binding_id}.{chat_id}``
  NATS subject the legacy endpoint used (subject reuse is
  intentional — the orchestrator's ``WebNatsGateway`` already
  subscribes wildcard ``web.stop.*.*`` and translates to its
  ``_on_stop`` callback; zero orch-side changes needed).
* ``binding_id`` and ``chat_id`` come from the **authenticated**
  handshake (ticket payload + conversation row), NOT from any
  client-supplied field on the frame. This was the explicit IDOR
  guard noted in the legacy implementation (``webui/ws.py:228-231``).
  A regression that started trusting payload fields would let a
  compromised client stop conversations they don't own.
* Optional ``run_id`` on the frame is logged for traceability but
  not used for routing — orch identifies the target container from
  ``binding_id+chat_id`` regardless.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from webui.v1 import ws_stream

pytestmark = pytest.mark.usefixtures("test_db")


# Shared with the other v1 WS test modules. See
# ``test_v1_ws_side_channel_subs.py`` for the env-var ordering
# rationale (``os.environ.setdefault`` first-wins).
_TEST_SECRET = "v1-ws-handshake-secret-only-for-tests"
os.environ.setdefault("WS_TICKET_SECRET", _TEST_SECRET)


@pytest.fixture(autouse=True)
def _pin_ws_ticket_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the app's verifier to THIS module's signing secret per test.

    ``setdefault`` is order-dependent — a sibling module collected first with
    a different secret wins the global env, making our valid tickets verify as
    4002 in a batch. ``ws_ticket._get_secret`` reads the env dynamically, so
    pinning per test makes signing and verification agree regardless of order;
    monkeypatch restores the prior value afterwards.
    """
    monkeypatch.setenv("WS_TICKET_SECRET", _TEST_SECRET)


# ---------------------------------------------------------------------------
# Fixtures — match the shape used by other v1 ws test files so the test
# infrastructure stays consistent. Lifted intentionally rather than moved
# to conftest.py: keeping each file self-contained makes failures easier
# to read without jumping between modules.
# ---------------------------------------------------------------------------


class _EmptySub:
    """Subscription that delivers nothing — the Stop test doesn't
    drive any NATS messages; we only need the WS handshake to complete
    and the receive loop to be alive for our ``request.stop`` frame
    to land."""

    def __init__(self) -> None:
        self.unsubscribed = False

    @property
    def messages(self):  # type: ignore[no-untyped-def]
        return self

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __anext__(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _CapturedJS:
    """Records publishes so the test can assert on the subject."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def subscribe(self, subject: str, **_kwargs: Any) -> _EmptySub:
        return _EmptySub()


@pytest.fixture
def js_stub():  # type: ignore[no-untyped-def]
    js = _CapturedJS()
    ws_stream.set_jetstream(js)  # type: ignore[arg-type]
    try:
        yield js
    finally:
        ws_stream.set_jetstream(None)


class _StubConv:
    def __init__(
        self, conv_id: str, tenant_id: str, binding_id: str, chat_id: str
    ) -> None:
        self.id = conv_id
        self.tenant_id = tenant_id
        self.channel_binding_id = binding_id
        self.channel_chat_id = chat_id


@pytest.fixture
def stub_conv(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    holder: dict[str, _StubConv] = {}

    async def _fake(conversation_id: str, *, tenant_id: str) -> _StubConv | None:
        return holder.get("conv")

    monkeypatch.setattr(ws_stream, "get_conversation", _fake)

    # The handshake also reads tenant lifecycle status now; stub it active
    # for the same sync-TestClient/asyncpg reason get_conversation is stubbed.
    async def _fake_status(_tenant_id: str) -> str:
        return "active"

    monkeypatch.setattr(ws_stream, "get_tenant_status", _fake_status)
    return holder


def _mint_ticket(*, tenant_id: str, conv_id: str, user_id: str) -> str:
    payload = {
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int((datetime.now(UTC) + timedelta(seconds=60)).timestamp()),
        "aud": "rolemesh-ws",
        "sub": user_id,
        "tenant_id": tenant_id,
        "conversation_id": conv_id,
    }
    return jwt.encode(payload, _TEST_SECRET, algorithm="HS256")


def _build_app() -> FastAPI:
    app = FastAPI()
    ws_stream.register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_request_stop_frame_publishes_to_web_stop_subject(
    js_stub: _CapturedJS, stub_conv: dict[str, _StubConv]
) -> None:
    """End-to-end happy path: SPA sends ``request.stop`` → v1 stream
    publishes to ``web.stop.{binding}.{chat}`` with the authenticated
    handshake identifiers. This is the contract the orchestrator's
    ``WebNatsGateway`` already consumes; subject reuse means zero
    orch-side changes are needed for PR-B's Stop migration.
    """
    conv_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())
    chat_id = "chat-stop-test"
    user_id = str(uuid.uuid4())
    stub_conv["conv"] = _StubConv(conv_id, tenant_id, binding_id, chat_id)

    ticket = _mint_ticket(
        tenant_id=tenant_id, conv_id=conv_id, user_id=user_id
    )
    app = _build_app()

    with TestClient(app) as client, client.websocket_connect(
        f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
    ) as ws:
        ws.send_json({
            "type": "request.stop",
            "run_id": str(uuid.uuid4()),  # advisory; not used for routing
        })
        # Sync barrier: send an unknown frame and wait for the
        # ``PROTOCOL_UNKNOWN_TYPE`` error reply. Because the WS
        # handler processes frames in order, receiving that error
        # guarantees the previous ``request.stop`` frame has
        # already been handled (and the NATS publish has landed
        # in the stub's recording list).
        ws.send_json({"type": "request.unknown_probe"})
        err = ws.receive_json()
        assert err.get("code") == "PROTOCOL_UNKNOWN_TYPE"

    # The Stop publish should have landed regardless of how the
    # probe frame was handled.
    stop_subject = f"web.stop.{binding_id}.{chat_id}"
    stop_publishes = [p for p in js_stub.published if p[0] == stop_subject]
    assert len(stop_publishes) == 1, (
        f"expected exactly one publish to {stop_subject}, got "
        f"{js_stub.published!r}. If you see zero, the request.stop "
        "branch in ws_stream.py regressed. If you see more, the "
        "loop is double-publishing."
    )
    # Payload is empty by contract — matches the legacy publisher at
    # webui/ws.py:232. Future versions may add fields, but today the
    # orch-side gateway only needs the subject's binding+chat parts.
    assert stop_publishes[0][1] == b"{}"


def test_request_stop_uses_authenticated_binding_not_client_payload(
    js_stub: _CapturedJS, stub_conv: dict[str, _StubConv]
) -> None:
    """IDOR guard: even if the client tries to override binding_id or
    chat_id in the payload, the handler must use the authenticated
    handshake values. The legacy implementation explicitly noted this
    (webui/ws.py:228-231). A regression that started trusting payload
    fields would let a compromised client stop conversations belonging
    to other users.
    """
    real_conv_id = str(uuid.uuid4())
    real_tenant_id = str(uuid.uuid4())
    real_binding = str(uuid.uuid4())
    real_chat = "real-chat"
    user_id = str(uuid.uuid4())
    stub_conv["conv"] = _StubConv(
        real_conv_id, real_tenant_id, real_binding, real_chat
    )

    ticket = _mint_ticket(
        tenant_id=real_tenant_id, conv_id=real_conv_id, user_id=user_id
    )
    app = _build_app()

    with TestClient(app) as client, client.websocket_connect(
        f"/api/v1/conversations/{real_conv_id}/stream?ticket={ticket}"
    ) as ws:
        ws.send_json({
            "type": "request.stop",
            # Hostile payload — these MUST be ignored.
            "binding_id": "ATTACKER-BINDING",
            "chat_id": "ATTACKER-CHAT",
            "run_id": str(uuid.uuid4()),
        })
        # Sync barrier — see test above for the rationale.
        ws.send_json({"type": "request.unknown_probe"})
        err = ws.receive_json()
        assert err.get("code") == "PROTOCOL_UNKNOWN_TYPE"

    # The publish must go to the REAL subject, not to anything derived
    # from the payload's binding_id/chat_id.
    real_subject = f"web.stop.{real_binding}.{real_chat}"
    attacker_subject = "web.stop.ATTACKER-BINDING.ATTACKER-CHAT"
    subjects = [p[0] for p in js_stub.published]
    assert real_subject in subjects
    assert attacker_subject not in subjects, (
        f"IDOR regression: publish landed at attacker-controlled subject "
        f"{attacker_subject!r}. Handler must use authenticated handshake "
        "identifiers, not payload fields. See webui/ws.py:228-231 for the "
        "legacy comment that established this contract."
    )


def test_request_stop_with_no_run_id_still_publishes(
    js_stub: _CapturedJS, stub_conv: dict[str, _StubConv]
) -> None:
    """``run_id`` is optional on the frame — it's advisory only. The
    SPA may not know a run_id yet (race between Stop click and the
    first ``event.run.started`` echo). Dropping the publish in that
    case would silently break Stop in the legitimate "user clicks
    very fast" scenario.
    """
    conv_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())
    chat_id = "chat-norunid"
    user_id = str(uuid.uuid4())
    stub_conv["conv"] = _StubConv(conv_id, tenant_id, binding_id, chat_id)

    ticket = _mint_ticket(
        tenant_id=tenant_id, conv_id=conv_id, user_id=user_id
    )
    app = _build_app()

    with TestClient(app) as client, client.websocket_connect(
        f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
    ) as ws:
        ws.send_json({"type": "request.stop"})  # no run_id field at all
        # Sync barrier — see first test for rationale.
        ws.send_json({"type": "request.unknown_probe"})
        err = ws.receive_json()
        assert err.get("code") == "PROTOCOL_UNKNOWN_TYPE"

    assert any(
        p[0] == f"web.stop.{binding_id}.{chat_id}" for p in js_stub.published
    )
