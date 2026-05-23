"""01b §PR2 — pinned test for the v1 WS handshake.

Covers the contract surface of
``WS /api/v1/conversations/{id}/stream?ticket=<jwt>``:

* Legal ticket — handshake accepts and the connection moves to the
  receive loop.
* Expired ticket — close code 4001, reason ``WS_TICKET_EXPIRED``.
* Invalid signature / malformed JWT — close code 4002, reason
  ``WS_TICKET_INVALID``.
* Ticket signed for conversation A, handshake on path /conversation/B —
  close code 4003 (no leak: existence isn't disclosed).
* Bootstrap multi-user (alice) — handshake accepts and the ticket
  ``sub`` is alice's real UUID, not the literal ``"bootstrap"``.

The tests stub the ``get_conversation`` DB lookup so the handshake
runs in starlette's TestClient threadpool without the asyncpg pool
crossing event loops (a known cross-loop issue with asyncpg + the
sync TestClient path). The handshake's load-bearing logic — ticket
verify, conversation_id binding, close-code mapping — does not depend
on the actual DB row content, only on its presence/absence; stubbing
the lookup exercises the same code paths without the cross-loop hazard.

Anti-mirror: the tests assert on close codes and ticket-payload claims,
not on the underlying ``verify_ws_ticket`` library calls. A regression
in the close-code mapping must surface here as a *value* mismatch.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rolemesh.auth.bootstrap_users import (
    BootstrapUserSpec,
    _reset_for_tests,
    ensure_bootstrap_user_row,
    init_bootstrap_users,
)
from webui.v1 import ws_stream

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# DB lookup stub — see module docstring for rationale.
# ---------------------------------------------------------------------------


class _StubConv:
    """Minimal duck-typed conversation used by the handshake.

    Only ``channel_binding_id`` and ``channel_chat_id`` are read in
    the handler before the receive loop starts; matching the real
    ``Conversation`` dataclass beyond those would be mirror-coding.
    """

    def __init__(
        self,
        conv_id: str,
        tenant_id: str,
        binding_id: str = "00000000-0000-0000-0000-000000000001",
        chat_id: str = "chat-stub",
    ) -> None:
        self.id = conv_id
        self.tenant_id = tenant_id
        self.channel_binding_id = binding_id
        self.channel_chat_id = chat_id


@pytest.fixture
def stub_get_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    """Patch the WS handler's ``get_conversation`` import.

    The fixture returns a control dict the test can mutate to set
    the lookup behaviour: keys ``return_none`` (force 4004 path) or
    ``conv_for_id`` (override per-conversation-id payload).
    """
    control: dict[str, object] = {"return_none": False, "conv_for_id": {}}

    async def _fake(
        conversation_id: str, *, tenant_id: str
    ) -> _StubConv | None:
        if control.get("return_none"):
            return None
        by_id = control.get("conv_for_id") or {}
        if conversation_id in by_id:  # type: ignore[operator]
            return by_id[conversation_id]  # type: ignore[index]
        return _StubConv(
            conv_id=conversation_id, tenant_id=tenant_id
        )

    monkeypatch.setattr(ws_stream, "get_conversation", _fake)
    return control


_TEST_SECRET = "v1-ws-handshake-secret-only-for-tests"
os.environ.setdefault("WS_TICKET_SECRET", _TEST_SECRET)


# ---------------------------------------------------------------------------
# Captured JetStream stub
# ---------------------------------------------------------------------------


class _CapturedSub:
    """Minimal NATS subscription with an empty messages iterator."""

    def __init__(self) -> None:
        self.unsubscribed = False

    @property
    def messages(self):  # type: ignore[no-untyped-def]
        return self  # iterator returns nothing

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __anext__(self):  # type: ignore[no-untyped-def]
        # Suspend forever so the forward task stays alive but emits
        # nothing — the WS handler's main loop is the focus here.
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _CapturedJS:
    """JetStream stub used by handshake tests.

    The publish path records calls so the post-handshake message
    flow tests (in 01b PR3 land) can inspect them. The subscribe
    path returns an empty subscription so ``_forward_stream``
    runs cleanly without a broker.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: list[_CapturedSub] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def subscribe(self, subject: str, **_kwargs: object) -> _CapturedSub:
        sub = _CapturedSub()
        self.subscriptions.append(sub)
        return sub


@pytest.fixture
def js_stub() -> _CapturedJS:
    js = _CapturedJS()
    ws_stream.set_jetstream(js)  # type: ignore[arg-type]
    try:
        yield js
    finally:
        ws_stream.set_jetstream(None)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    ws_stream.register_routes(app)
    return app


def _fresh_conv_id() -> str:
    """Generate a fresh UUID for use as a conversation id.

    The handshake stubs out ``get_conversation`` so no row needs to
    exist — the id only has to be a syntactically valid UUID for
    the ticket payload to bind against.
    """
    return str(uuid.uuid4())


def _sign_ticket(
    *,
    user_id: str,
    tenant_id: str,
    conversation_id: str,
    ttl_seconds: int = 60,
    secret: str = _TEST_SECRET,
) -> str:
    """Mint a ticket with the same shape ``ws_ticket.issue_ws_ticket`` does.

    Reusing the production issuer would couple the handshake test
    to the issuer's clock-clamp behaviour; signing directly lets a
    test exercise the rare "negative ttl" and "wrong audience"
    cases without monkeypatching.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "aud": "rolemesh-ws",
        "sub": user_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Legal ticket — handshake succeeds
# ---------------------------------------------------------------------------


def test_legal_ticket_handshake_accepts(
    js_stub: _CapturedJS,
    stub_get_conversation: dict[str, object],
) -> None:
    tid = str(uuid.uuid4())
    conv_id = _fresh_conv_id()
    ticket = _sign_ticket(
        user_id=str(uuid.uuid4()),
        tenant_id=tid,
        conversation_id=conv_id,
    )
    app = _build_app()
    client = TestClient(app)
    with client.websocket_connect(
        f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
    ) as ws:
        # If we got here, the handshake accepted. Close cleanly.
        ws.close()


# ---------------------------------------------------------------------------
# Expired ticket -> 4001
# ---------------------------------------------------------------------------


def test_expired_ticket_closes_with_4001() -> None:
    tid = str(uuid.uuid4())
    conv_id = _fresh_conv_id()
    # ttl_seconds=-5 mints an already-expired ticket (the issuer's
    # clamp at [1, 60] would refuse this, which is why the test
    # bypasses the issuer and signs directly).
    ticket = _sign_ticket(
        user_id=str(uuid.uuid4()),
        tenant_id=tid,
        conversation_id=conv_id,
        ttl_seconds=-5,
    )
    app = _build_app()
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
        ):
            pass
    except WebSocketDisconnect as exc:
        assert exc.code == 4001
        assert exc.reason == "WS_TICKET_EXPIRED"
        return
    raise AssertionError("expected WebSocketDisconnect with code 4001")


# ---------------------------------------------------------------------------
# Invalid signature -> 4002
# ---------------------------------------------------------------------------


def test_invalid_signature_closes_with_4002() -> None:
    tid = str(uuid.uuid4())
    conv_id = _fresh_conv_id()
    # Sign with a different secret so the verify path raises
    # InvalidTokenError.
    ticket = _sign_ticket(
        user_id=str(uuid.uuid4()),
        tenant_id=tid,
        conversation_id=conv_id,
        secret="wrong-secret",
    )
    app = _build_app()
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
        ):
            pass
    except WebSocketDisconnect as exc:
        assert exc.code == 4002
        assert exc.reason == "WS_TICKET_INVALID"
        return
    raise AssertionError("expected WebSocketDisconnect with code 4002")


def test_missing_ticket_closes_with_4002() -> None:
    """Empty ``ticket`` query parameter is the same shape as a malformed JWT."""
    _tid = str(uuid.uuid4())
    conv_id = _fresh_conv_id()
    app = _build_app()
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream"
        ):
            pass
    except WebSocketDisconnect as exc:
        assert exc.code == 4002
        return
    raise AssertionError("expected WebSocketDisconnect with code 4002")


# ---------------------------------------------------------------------------
# Ticket vs path mismatch -> 4003
# ---------------------------------------------------------------------------


def test_ticket_conversation_id_mismatch_closes_with_4003() -> None:
    """Signed for conv A, opening on conv B path — must reject.

    Without this check, any user with a valid ticket for *one*
    conversation could attach to any conversation UUID they happen
    to know in the tenant. The ticket binding is the only thing
    making the WS endpoint safe at handshake time.
    """
    tid = str(uuid.uuid4())
    conv_a = _fresh_conv_id()
    conv_b = _fresh_conv_id()
    ticket = _sign_ticket(
        user_id=str(uuid.uuid4()),
        tenant_id=tid,
        conversation_id=conv_a,
    )
    app = _build_app()
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(
            f"/api/v1/conversations/{conv_b}/stream?ticket={ticket}"
        ):
            pass
    except WebSocketDisconnect as exc:
        assert exc.code == 4003, f"expected 4003, got {exc.code}"
        return
    raise AssertionError("expected WebSocketDisconnect with code 4003")


# ---------------------------------------------------------------------------
# Bootstrap multi-user — alice's real UUID lands in the ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_multi_user_alice_handshake_uses_real_uuid(
    js_stub: _CapturedJS,
    stub_get_conversation: dict[str, object],
) -> None:
    """Tickets issued for alice carry her real UUID.

    The handshake itself is the focus: the ticket bears alice's
    real UUID (not the literal ``"bootstrap"``), so any downstream
    code that reads ``payload.user_id`` to attribute traffic gets
    a real principal. We sign the ticket ourselves to keep the
    issuer side simple — the issuer is exercised in
    ``test_v1_auth_endpoints``.
    """
    from rolemesh.db import create_tenant

    # ``ensure_bootstrap_user_row`` needs a real tenant row to FK to;
    # everything else in this test runs on synthetic UUIDs.
    t = await create_tenant(
        name=f"T-{uuid.uuid4().hex[:6]}",
        slug=f"ws-alice-{uuid.uuid4().hex[:6]}",
    )
    tid = t.id
    conv_id = _fresh_conv_id()
    _reset_for_tests()
    init_bootstrap_users(
        env_value=(
            '[{"token":"tok-alice","user_id":"alice","tenant":"default",'
            '"role":"owner"}]'
        )
    )
    spec = BootstrapUserSpec(
        token="tok-alice",
        user_id_slug="alice",
        tenant_slug="default",
        role="owner",
    )
    alice_uuid = await ensure_bootstrap_user_row(spec, tid)
    assert len(alice_uuid) == 36, "alice must land as a real UUID"

    ticket = _sign_ticket(
        user_id=alice_uuid, tenant_id=tid, conversation_id=conv_id
    )
    decoded = jwt.decode(
        ticket, _TEST_SECRET, algorithms=["HS256"], audience="rolemesh-ws"
    )
    assert decoded["sub"] == alice_uuid

    app = _build_app()
    client = TestClient(app)
    with client.websocket_connect(
        f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
    ) as ws:
        ws.close()
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Conversation that doesn't exist (post-ticket-verify) -> 4004
# ---------------------------------------------------------------------------


def test_ticket_valid_but_conversation_deleted_returns_4004(
    js_stub: _CapturedJS,
    stub_get_conversation: dict[str, object],
) -> None:
    """A ticket can outlive its conversation if the row is DELETEd.

    The handshake validates the *current* DB state, not the
    ticket's snapshot, so a stale ticket attached after the
    conversation was deleted closes 4004. Forging this would also
    be caught by RLS but the 4004 lets the SPA distinguish
    "conversation gone" from "auth failure".
    """
    tid = str(uuid.uuid4())
    conv_id = _fresh_conv_id()
    ticket = _sign_ticket(
        user_id=str(uuid.uuid4()),
        tenant_id=tid,
        conversation_id=conv_id,
    )
    # Simulate the row having been DELETEd between ticket mint and
    # WS connect by forcing the stubbed lookup to return None.
    stub_get_conversation["return_none"] = True
    app = _build_app()
    client = TestClient(app)
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
        ):
            pass
    except WebSocketDisconnect as exc:
        assert exc.code == 4004
        return
    raise AssertionError("expected WebSocketDisconnect with code 4004")
