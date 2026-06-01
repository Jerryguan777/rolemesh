"""v1 WS must forward ``web.outbound.*`` and ``web.stream.* kind=status``.

Background — v1.1 refactor (2026-05-20) created two silent gaps:

* Legacy ``/ws/chat`` subscribed to ``web.stream.*`` AND
  ``web.outbound.*``. ``web.outbound`` carries complete agent replies
  that don't fit the streaming chunk path — canonical example: a
  scheduled-task reminder fires outside any user request.run, so
  there's no streaming context for the reply to attach to.
* Legacy's ``web.stream.*`` handler routed FOUR kinds — ``text``,
  ``done``, ``status``, ``safety_blocked``. v1 ``ws_stream.py`` only
  handled three; the ``status`` branch was dropped, so the
  per-turn progress indicators (``tool_use`` with the tool name,
  ``container_starting``, ``queued``, ``running``) silently vanished
  even though the orchestrator kept publishing them. The SPA's
  "Calling Read…" / "Starting container…" labels stopped rendering.

This file pins:

1. ``web.outbound.{binding}.{chat}`` publishes arrive as
   ``event.message.appended`` frames. The frame is run-id-free by
   design — out-of-band agent messages aren't tied to a user-initiated
   run lifecycle, so forcing a run_id would require synthesising a
   fake one or dropping the message.
2. ``web.stream.{binding}.{chat}`` ``type=status`` payloads arrive as
   ``event.run.progress`` frames AND are dropped when no
   ``active_run_id`` is set. Progress without a run has no meaningful
   client side-effect — the indicator is anchored to the running bubble.

A regression that re-drops either branch would surface here
immediately.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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


# Shared with tests/webui/test_ws_v1_handshake.py — both files use
# ``os.environ.setdefault`` so the first to import wins. Using the
# same secret string keeps the two test modules interoperable when
# pytest loads them in either order (alphabetical collection puts
# this file first, which used to break the handshake tests when
# the secrets diverged).
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
# Pure builders — exercised directly so regressions in shape surface fast
# ---------------------------------------------------------------------------


def test_build_outbound_frame_has_required_shape() -> None:
    """The frame must carry the canonical 4 fields the SPA renders. A
    rename / drop here breaks the chat-panel handler silently — there's
    no runtime validator on outgoing frames (see ``_send_event``)."""
    frame = ws_stream._build_outbound_frame(
        text="⏰ reminder", timestamp="2026-05-31T12:00:00+00:00"
    )
    assert frame == {
        "type": "event.message.appended",
        "content": "⏰ reminder",
        "source": "scheduled_task",
        "timestamp": "2026-05-31T12:00:00+00:00",
    }


def test_build_progress_frame_or_none_returns_none_without_run() -> None:
    """No active_run_id → no frame. The SPA's progress indicator is
    anchored to a run; emitting frames without one would either crash
    the renderer (run_id is required by the discriminated union) or
    force the SPA to handle an orphan state. Dropping at the source
    is the only sane semantic."""
    assert (
        ws_stream._build_progress_frame_or_none(None, {"status": "tool_use"})
        is None
    )


def test_build_progress_frame_or_none_returns_none_for_payload_without_status() -> None:
    """Malformed payload (missing or non-string ``status`` field) is
    treated the same as "no progress to forward". Without this guard
    the orch's send_status with an empty payload would emit a frame
    with no status string and the SPA's renderer would have nothing
    to map to a label."""
    assert ws_stream._build_progress_frame_or_none("run-1", {}) is None
    assert (
        ws_stream._build_progress_frame_or_none("run-1", {"status": ""}) is None
    )
    assert (
        ws_stream._build_progress_frame_or_none("run-1", {"status": 123}) is None
    )


def test_build_progress_frame_or_none_returns_bare_frame_for_running() -> None:
    """Status payloads without tool metadata produce a minimal frame:
    just type / run_id / status. ``tool`` and ``input_preview`` are
    omitted (not nulled) so the wire stays compact and the SPA's
    "key in object" checks work."""
    frame = ws_stream._build_progress_frame_or_none(
        "run-abc", {"status": "running"}
    )
    assert frame == {
        "type": "event.run.progress",
        "run_id": "run-abc",
        "status": "running",
    }


def test_build_progress_frame_or_none_returns_enriched_frame_for_tool_use() -> None:
    """``tool_use`` payloads carry tool name and an input preview from
    agent_runner's ``ToolUseEvent`` metadata. Both fields are pulled
    through; the truncation-semantics renaming (``input`` →
    ``input_preview``) makes it explicit to the SPA that the preview
    is not the full input."""
    frame = ws_stream._build_progress_frame_or_none(
        "run-abc",
        {"status": "tool_use", "tool": "Read", "input": "file=README.md ...(trunc)"},
    )
    assert frame == {
        "type": "event.run.progress",
        "run_id": "run-abc",
        "status": "tool_use",
        "tool": "Read",
        "input_preview": "file=README.md ...(trunc)",
    }


def test_build_progress_frame_or_none_ignores_non_string_tool_or_input() -> None:
    """Defensive filtering: orch publishes from a dict[str, object]
    so a metadata field could be any type. The forwarder must not
    coerce — drop the field instead so a junk value never reaches
    the SPA's TS-typed handler."""
    frame = ws_stream._build_progress_frame_or_none(
        "run-abc",
        {"status": "tool_use", "tool": 123, "input": None},
    )
    assert frame == {
        "type": "event.run.progress",
        "run_id": "run-abc",
        "status": "tool_use",
    }


# ---------------------------------------------------------------------------
# Integration: drive a preloaded NATS message through subscribe → WS frame
# ---------------------------------------------------------------------------


class _FakeMsg:
    """Stand-in for ``nats.aio.msg.Msg``. Only the two attributes the
    forward loop touches are populated (``data`` is consumed via
    ``json.loads``; ``ack`` is awaited)."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def ack(self) -> None:
        return None


class _PreloadedSub:
    """Subscription stub that delivers a fixed list of messages then
    suspends forever. Mirrors how a real ephemeral JetStream consumer
    behaves: returns whatever's queued when iterated, then blocks
    waiting for the next publish. The infinite sleep keeps the
    forwarding task alive without busy-spinning so the WS handler's
    main loop can run undisturbed."""

    def __init__(self, preloaded: list[_FakeMsg]) -> None:
        self._queued = list(preloaded)
        self.unsubscribed = False

    @property
    def messages(self):  # type: ignore[no-untyped-def]
        return self

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __anext__(self):  # type: ignore[no-untyped-def]
        if self._queued:
            return self._queued.pop(0)
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _ControlledJS:
    """JetStream stub that routes ``subscribe(subject)`` to per-subject
    preloaded message lists. Tests stuff the list before connecting
    the WS so messages are "already there" when the forward task
    starts iterating — avoids the cross-event-loop hazards that come
    with using ``asyncio.Queue`` between TestClient's WS thread and
    the test body."""

    def __init__(self) -> None:
        self.preloaded: dict[str, list[_FakeMsg]] = {}
        self.published: list[tuple[str, bytes]] = []
        self.subs: dict[str, _PreloadedSub] = {}

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def subscribe(self, subject: str, **_kwargs: Any) -> _PreloadedSub:
        sub = _PreloadedSub(self.preloaded.get(subject, []))
        self.subs[subject] = sub
        return sub


@pytest.fixture
def js_stub():  # type: ignore[no-untyped-def]
    js = _ControlledJS()
    ws_stream.set_jetstream(js)  # type: ignore[arg-type]
    try:
        yield js
    finally:
        ws_stream.set_jetstream(None)


class _StubConv:
    """Minimal duck-typed conversation; mirrors ``test_ws_v1_handshake``
    pattern. Only the four fields the handler reads."""

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
    return holder


def _mint_ticket(*, tenant_id: str, conv_id: str, user_id: str) -> str:
    payload = {
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int((datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()),
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


def _make_bootstrap_user(tenant_id: str, user_id: str) -> None:
    """Seed a bootstrap user row so the ticket's ``sub`` resolves
    against ``users``. The handshake doesn't query this directly but
    downstream code in ``register_routes`` may; copy the pattern
    used in ``test_ws_v1_handshake``."""
    _reset_for_tests()
    init_bootstrap_users(
        [BootstrapUserSpec(token="t", name="U", tenant_slug=f"sl-{user_id[:6]}")]
    )


def test_web_outbound_publish_is_forwarded_as_event_message_appended(
    js_stub: _ControlledJS,
    stub_conv: dict[str, _StubConv],
) -> None:
    """End-to-end happy path: orchestrator publishes a scheduled-task
    reply on ``web.outbound.{binding}.{chat}`` → v1 WS subscriber
    receives the bytes → forward task emits an
    ``event.message.appended`` frame on the SPA's WebSocket.

    Pre-fix, no such subscription existed: the publish landed at NATS
    with no consumer, JetStream stored it (a separate non-issue), and
    the SPA never saw it.
    """
    conv_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())
    chat_id = "chat-outbound-test"
    user_id = str(uuid.uuid4())
    stub_conv["conv"] = _StubConv(conv_id, tenant_id, binding_id, chat_id)

    # Preload BEFORE the WS connects — subscribe() happens inside the
    # handshake. The preloaded message will be delivered as soon as
    # _forward_outbound starts iterating.
    js_stub.preloaded[f"web.outbound.{binding_id}.{chat_id}"] = [
        _FakeMsg(json.dumps({"text": "⏰ reminder fired"}).encode())
    ]

    ticket = _mint_ticket(
        tenant_id=tenant_id, conv_id=conv_id, user_id=user_id
    )
    app = _build_app()

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/v1/conversations/{conv_id}/stream?ticket={ticket}"
        ) as ws:
            frame = ws.receive_json()
            assert frame["type"] == "event.message.appended", (
                f"expected event.message.appended, got {frame!r}. "
                "If you see this fail with no frame received at all, "
                "the v1 ws_stream regressed and stopped subscribing to "
                "web.outbound.* — the original 2026-05-20 v1.1 cutover "
                "bug returned."
            )
            assert frame["content"] == "⏰ reminder fired"
            assert frame["source"] == "scheduled_task"
            assert "timestamp" in frame
