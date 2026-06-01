"""``WS /api/v1/conversations/{id}/stream`` — design §4 protocol.

Stands alongside the legacy :mod:`webui.ws` endpoint (``/ws/chat``);
both ship in parallel during the migration window. The v1 path is
the canonical surface for the SPA going forward.

The shape:

* Handshake — verify the short-lived JWT ticket
  (:mod:`rolemesh.auth.ws_ticket`) before any DB work. Failure
  closes the WS with a 4001/4002/4003 code so the SPA can branch
  on the reason without re-reading the body.
* client → server frames:
  - ``request.run`` — initiates a new agent invocation.
    ``idempotency_key`` is *required* (per 01b lockdown). A
    duplicate inside the 60s window returns the same ``run_id``
    without re-publishing to NATS.
  - ``request.cancel`` — fire-and-forget; the actual
    ``status='cancelled'`` write happens via the orchestrator
    (no ghost-container risk — see :mod:`webui.v1.run_events`).
* server → client events: a thin pass-through over the existing
  ``web.stream.{binding_id}.{chat_id}`` topics, projecting them
  into ``event.run.*`` frames keyed by the active ``run_id``.

Disconnect semantics: per 01b Open Question 1 (locked), a client
closing the WS does NOT cancel the active run. Only an explicit
``request.cancel`` / POST ``/api/v1/runs/{id}/cancel`` does. The
fire-and-forget design lets the agent finish its work even after
the browser tab is closed; the next reconnect calls
``GET /api/v1/runs/{id}`` to fetch truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Query, WebSocket, WebSocketDisconnect
from nats.js.api import DeliverPolicy
from starlette.websockets import WebSocketState

from rolemesh.auth.ws_ticket import (
    WsTicketError,
    WsTicketExpired,
    WsTicketPayload,
    verify_ws_ticket,
)
from rolemesh.core.logger import get_logger
from rolemesh.db import (
    get_conversation,
    store_message,
    tenant_conn,
)
from rolemesh.ipc.web_protocol import WebInboundMessage
from rolemesh.runs import (
    create_run,
    terminate_run_via_ws_completed,
    terminate_run_via_ws_error,
)
from webui.v1.idempotency import cache as idempotency_cache
from webui.v1.run_events import publish_run_cancel

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext

logger = get_logger()


# ---------------------------------------------------------------------------
# WS close codes (RFC 6455 private-use range 4000-4999)
# ---------------------------------------------------------------------------

_CLOSE_TICKET_EXPIRED = 4001
_CLOSE_TICKET_INVALID = 4002
_CLOSE_TICKET_MISMATCH = 4003
_CLOSE_NOT_FOUND = 4004


# ---------------------------------------------------------------------------
# Module-level JetStream context — set by webui.main.lifespan
# ---------------------------------------------------------------------------


_js: "JetStreamContext | None" = None


def set_jetstream(js: "JetStreamContext | None") -> None:
    """Attach or detach the process-wide JetStream context."""
    global _js
    _js = js


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


async def _verify_handshake(
    ws: WebSocket, conversation_id: str, ticket: str
) -> WsTicketPayload | None:
    """Validate the ticket against the WS path.

    Returns the decoded payload on success, or ``None`` after
    closing the WS with the appropriate close code. The
    ticket→path mismatch check is deliberately split from the
    expiry / signature checks so the SPA can distinguish "your
    session expired" (re-request a ticket) from "you don't own
    this conversation" (don't bother retrying — different bug).
    """
    if not ticket:
        await ws.close(code=_CLOSE_TICKET_INVALID, reason="WS_TICKET_INVALID")
        return None
    try:
        payload = verify_ws_ticket(ticket)
    except WsTicketExpired:
        await ws.close(code=_CLOSE_TICKET_EXPIRED, reason="WS_TICKET_EXPIRED")
        return None
    except WsTicketError:
        await ws.close(code=_CLOSE_TICKET_INVALID, reason="WS_TICKET_INVALID")
        return None
    if payload.conversation_id != conversation_id:
        await ws.close(
            code=_CLOSE_TICKET_MISMATCH,
            reason="ticket conversation mismatch",
        )
        return None
    return payload


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


async def _send_event(ws: WebSocket, frame: dict[str, Any]) -> None:
    """Send a JSON frame guarded against double-close races."""
    if ws.client_state != WebSocketState.CONNECTED:
        return
    with contextlib.suppress(OSError, RuntimeError, WebSocketDisconnect):
        await ws.send_json(frame)


def _build_outbound_frame(*, text: str, timestamp: str) -> dict[str, Any]:
    """Build an ``event.message.appended`` frame for an out-of-band
    agent reply (scheduled-task reminder, future cross-chat
    notification, etc).

    Deliberately carries no ``run_id``: out-of-band messages aren't
    bound to a user-initiated request.run lifecycle, and synthesising
    a fake one would pollute the runs table on the SPA side. The
    chat-panel renders these the same way it renders messages fetched
    from ``GET /api/v1/conversations/{id}/messages`` on reload.
    """
    return {
        "type": "event.message.appended",
        "content": text,
        "source": "scheduled_task",
        "timestamp": timestamp,
    }


def _build_progress_frame_or_none(
    active_run_id: str | None, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Build an ``event.run.progress`` frame from a status payload, or
    ``None`` when there's no active run to anchor it to.

    The orchestrator publishes per-turn progress indicators
    (``running`` / ``tool_use`` / ``queued`` / ``container_starting``)
    on ``web.stream.{...}`` as a ``kind="status"`` chunk whose
    ``content`` carries a JSON-serialised payload like
    ``{"status": "tool_use", "tool": "Read", "input": "..."}``.
    Legacy ``/ws/chat`` forwarded this; v1 dropped the branch and the
    SPA stopped seeing "Calling Read…" / "Starting container…" labels.

    Restore the path here with explicit field whitelisting so a future
    metadata addition on the orch side doesn't accidentally leak
    internal-only keys to the browser. ``tool`` and ``input_preview``
    are populated only for ``tool_use`` payloads (matches the
    ``ToolUseEvent`` metadata shape in agent_runner.main).
    """
    if active_run_id is None:
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None
    frame: dict[str, Any] = {
        "type": "event.run.progress",
        "run_id": active_run_id,
        "status": status,
    }
    tool = payload.get("tool")
    if isinstance(tool, str) and tool:
        frame["tool"] = tool
    # agent_runner publishes the truncated preview under ``input``
    # (see ToolUseEvent → ContainerOutput.metadata). Rename here so
    # the wire field name carries the truncation semantics explicitly
    # — the SPA shouldn't think it's getting the full input.
    input_preview = payload.get("input")
    if isinstance(input_preview, str) and input_preview:
        frame["input_preview"] = input_preview
    return frame


def _build_approval_frame_or_none(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Project an orchestrator ``web.approval.*`` payload to a client frame.

    Whitelist fields explicitly (same posture as the progress branch) so a
    future orchestrator-side key can't leak to the browser. ``requested`` →
    ``event.approval.requested`` (carries the card data); ``resolved`` →
    ``event.approval.resolved`` (the deterministic terminal state). Anything
    else returns ``None`` and is dropped.
    """
    kind = payload.get("type")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    if kind == "approval.requested":
        frame: dict[str, Any] = {
            "type": "event.approval.requested",
            "request_id": request_id,
        }
        summary = payload.get("action_summary")
        if isinstance(summary, str):
            frame["action_summary"] = summary
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, str):
            frame["expires_at"] = expires_at
        return frame
    if kind == "approval.resolved":
        outcome = payload.get("outcome")
        if outcome not in ("approved", "rejected", "expired"):
            return None
        return {
            "type": "event.approval.resolved",
            "request_id": request_id,
            "outcome": outcome,
        }
    return None


async def _terminate_run_completed(
    *, run_id: str, tenant_id: str, usage: Any | None
) -> None:
    """Fire INV-6 path 1 (ws_completed) inside a tenant-scoped txn.

    Wrapped in :class:`contextlib.suppress` so a transient DB
    hiccup never crashes the forwarding loop — the run row stays
    ``running`` and a future GET ``/api/v1/runs/{id}`` will report
    the (now-incorrect) state until the operator notices. The
    lifecycle helper's ``WHERE status='running'`` guard makes the
    UPDATE idempotent in case the NATS chunk redelivers and we run
    here twice.
    """
    usage_dict: dict[str, Any] | None = (
        usage if isinstance(usage, dict) else None
    )
    try:
        async with tenant_conn(tenant_id) as conn:
            await terminate_run_via_ws_completed(
                run_id=run_id, usage=usage_dict, conn=conn
            )
    except Exception:
        logger.warning(
            "ws_stream: terminator UPDATE failed (run stays 'running'); "
            "investigate orchestrator side",
            run_id=run_id,
            exc_info=True,
        )


async def _terminate_run_errored(
    *, run_id: str, tenant_id: str, error: dict[str, Any]
) -> None:
    """Symmetric helper for INV-6 path 2 (ws_error)."""
    try:
        async with tenant_conn(tenant_id) as conn:
            await terminate_run_via_ws_error(
                run_id=run_id, error=error, conn=conn
            )
    except Exception:
        logger.warning(
            "ws_stream: error terminator UPDATE failed",
            run_id=run_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


async def stream(
    ws: WebSocket,
    conversation_id: str,
    ticket: str = Query("", alias="ticket"),
) -> None:
    """WS endpoint per design §4. Mounted on the FastAPI app.

    Mounting via :func:`register_routes` (see end of file) so the
    composition test in ``webui.api_v1`` can swap fixtures without
    importing the FastAPI app graph directly.
    """
    payload = await _verify_handshake(ws, conversation_id, ticket)
    if payload is None:
        return

    # Conversation existence + tenant match — the ticket payload
    # already binds the conversation, but RLS still requires the
    # row to be present in the caller's tenant. Treat absence as
    # 4004 so the SPA distinguishes "ticket OK but row missing" from
    # auth issues; both forms of failure look the same to an
    # attacker but the operator sees the difference in logs.
    import asyncpg

    try:
        conv = await get_conversation(
            conversation_id, tenant_id=payload.tenant_id
        )
    except asyncpg.DataError:
        # Bad UUID syntax — collapse to 4004 to avoid leaking the
        # parser hint to clients.
        conv = None
    if conv is None:
        await ws.close(code=_CLOSE_NOT_FOUND, reason="conversation not found")
        return

    await ws.accept()

    # ``binding_id`` is needed for the NATS subjects the
    # orchestrator emits onto. The conversation row carries it
    # directly so no extra DB call beyond ``get_conversation``.
    binding_id = conv.channel_binding_id

    # Active run state. The lifecycle helper's
    # ``WHERE status='running'`` gate is the only enforcement of
    # "one active run per conversation"; this variable tracks the
    # *current* one so we can stamp ``event.run.*`` frames.
    active_run_id: str | None = None
    # Active-run lock — guards the create-run + idempotency probe
    # against two ``request.run`` frames arriving back-to-back
    # before the first NATS publish lands.
    active_run_lock = asyncio.Lock()

    # JetStream subscriptions
    if _js is None:
        await ws.close(code=1011, reason="server jetstream not initialised")
        return
    js = _js

    stream_sub = await js.subscribe(
        f"web.stream.{binding_id}.{conv.channel_chat_id}",
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )
    # ``web.outbound.*`` carries complete agent replies that bypass
    # the streaming path — today's only producer is the scheduled-task
    # send_message IPC bridge in ``rolemesh.main``. Legacy ``/ws/chat``
    # subscribed here; v1 missed it during the 2026-05-20 cutover, so
    # scheduled-task reminders only appeared after a page reload
    # (DB persistence kept the message; live push was silently dropped).
    outbound_sub = await js.subscribe(
        f"web.outbound.{binding_id}.{conv.channel_chat_id}",
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )
    # ``web.approval.*`` carries HITL approval cards + their hard-channel
    # resolution. Independent of ``active_run_id`` (an approval can outlive the
    # run that triggered it, and scheduled-task approvals have no run at all),
    # so it rides its own subject like ``web.outbound``.
    approval_sub = await js.subscribe(
        f"web.approval.{binding_id}.{conv.channel_chat_id}",
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )

    async def _forward_stream() -> None:
        """Fan NATS stream chunks to ``event.run.*`` frames.

        The legacy ``web.stream.*`` carrier is reused here so this
        endpoint can interoperate with the existing orchestrator
        emitter — replacing the emitter would be another session.
        ``run_id`` is stamped from the closure's ``active_run_id``;
        when ``None``, the frame is dropped because no client
        side-effect could meaningfully consume it.

        INV-6 happy-path UPDATE: on ``done`` / ``safety_blocked`` the
        terminator fires here so ``runs.{status, completed_at}`` lands
        in the DB. Disconnect-mid-turn still leaves the row at
        ``running`` (the ``finally`` in :func:`stream` cancels this
        task — the WS handler does not survive a closed socket). A
        durable orchestrator-side terminator for that case is on the
        backlog (see 01c smoke Findings — design §11 INV-6 path 1).
        """
        async for msg in stream_sub.messages:
            try:
                data = json.loads(msg.data)
                kind = data.get("type")
                run_id = active_run_id
                if run_id is None:
                    await msg.ack()
                    continue
                if kind == "text":
                    await _send_event(
                        ws,
                        {
                            "type": "event.run.token",
                            "run_id": run_id,
                            "delta": data.get("content", ""),
                        },
                    )
                elif kind == "done":
                    # INV-6 path 1: write the terminal status FIRST so
                    # the UPDATE survives even if the client closes
                    # the WS the moment it sees ``event.run.completed``
                    # (the disconnect cancels ``_forward_stream`` —
                    # without ordering this before ``_send_event``, a
                    # fast-close races the cancellation against the
                    # DB write and the row stays at ``running``). We
                    # also wrap in ``asyncio.shield`` so cancellation
                    # mid-UPDATE doesn't strand the row. Lifecycle
                    # helper is idempotent on the ``WHERE
                    # status='running'`` guard, so re-delivery is safe.
                    await asyncio.shield(
                        _terminate_run_completed(
                            run_id=run_id,
                            tenant_id=payload.tenant_id,
                            usage=data.get("usage"),
                        )
                    )
                    await _send_event(
                        ws,
                        {
                            "type": "event.run.completed",
                            "run_id": run_id,
                        },
                    )
                elif kind == "status":
                    # Per-turn progress indicator (running / tool_use /
                    # queued / container_starting). Legacy ``/ws/chat``
                    # forwarded these; the v1 cutover dropped the
                    # branch, so the SPA stopped seeing "Calling Read…"
                    # and "Starting container…" labels even though the
                    # orchestrator kept publishing them. See
                    # ``_build_progress_frame_or_none`` for the wire
                    # contract — None is returned when no run is
                    # active OR the payload lacks a ``status`` field.
                    inner = json.loads(data.get("content", "{}"))
                    progress = _build_progress_frame_or_none(run_id, inner)
                    if progress is not None:
                        await _send_event(ws, progress)
                elif kind == "safety_blocked":
                    inner = json.loads(data.get("content", "{}"))
                    # Same ordering rationale as ``done`` above.
                    await asyncio.shield(
                        _terminate_run_errored(
                            run_id=run_id,
                            tenant_id=payload.tenant_id,
                            error={
                                "code": "SAFETY_BLOCKED",
                                "message": inner.get("reason") or "blocked",
                                "stage": inner.get("stage"),
                                "rule_id": inner.get("rule_id"),
                            },
                        )
                    )
                    await _send_event(
                        ws,
                        {
                            "type": "event.run.error",
                            "run_id": run_id,
                            "code": "SAFETY_BLOCKED",
                            "message": inner.get("reason") or "blocked",
                            "details": inner,
                        },
                    )
                await msg.ack()
            except (WebSocketDisconnect, RuntimeError):
                return
            except (OSError, ValueError, TypeError, KeyError):
                with contextlib.suppress(OSError, RuntimeError):
                    await msg.ack()

    async def _forward_outbound() -> None:
        """Fan ``web.outbound.*`` payloads to ``event.message.appended``
        frames. Independent of ``active_run_id`` — these messages are
        agent-initiated side-channel deliveries (scheduled-task
        reminders today; cross-chat notifications in the future) and
        don't belong to any user-initiated run. See
        ``_build_outbound_frame`` for the frame contract rationale.
        """
        async for msg in outbound_sub.messages:
            try:
                data = json.loads(msg.data)
                text = data.get("text")
                if isinstance(text, str) and text:
                    await _send_event(
                        ws,
                        _build_outbound_frame(
                            text=text,
                            timestamp=datetime.now(UTC).isoformat(),
                        ),
                    )
                await msg.ack()
            except (WebSocketDisconnect, RuntimeError):
                return
            except (OSError, ValueError, TypeError, KeyError):
                with contextlib.suppress(OSError, RuntimeError):
                    await msg.ack()

    async def _forward_approval() -> None:
        """Fan ``web.approval.*`` payloads to ``event.approval.*`` frames.

        Independent of ``active_run_id`` — see ``_build_approval_frame_or_none``
        for the wire contract and field whitelisting.
        """
        async for msg in approval_sub.messages:
            try:
                data = json.loads(msg.data)
                frame = _build_approval_frame_or_none(data)
                if frame is not None:
                    await _send_event(ws, frame)
                await msg.ack()
            except (WebSocketDisconnect, RuntimeError):
                return
            except (OSError, ValueError, TypeError, KeyError):
                with contextlib.suppress(OSError, RuntimeError):
                    await msg.ack()

    fwd_tasks = [
        asyncio.create_task(_forward_stream()),
        asyncio.create_task(_forward_outbound()),
        asyncio.create_task(_forward_approval()),
    ]

    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await _send_event(
                    ws,
                    {
                        "type": "event.run.error",
                        "code": "PROTOCOL_BAD_JSON",
                        "message": "frame must be valid JSON",
                    },
                )
                continue
            kind = frame.get("type")
            if kind == "request.run":
                active_run_id = await _handle_request_run(
                    ws=ws,
                    frame=frame,
                    payload=payload,
                    conv=conv,
                    binding_id=binding_id,
                    js=js,
                    active_run_lock=active_run_lock,
                )
            elif kind == "request.cancel":
                await _handle_request_cancel(
                    ws=ws, frame=frame, payload=payload,
                )
            elif kind == "request.stop":
                # Interrupt the currently-running agent turn for this
                # conversation. The orchestrator's WebNatsGateway
                # subscribes ``web.stop.*.*`` and identifies the
                # target container from binding+chat (NOT from the
                # frame payload — IDOR guard). The frame's optional
                # ``run_id`` is advisory and only logged. Empty body
                # matches the legacy publisher contract
                # (webui/ws.py:232) so the orch-side receiver needs
                # no changes for the v1 migration.
                await js.publish(
                    f"web.stop.{binding_id}.{conv.channel_chat_id}",
                    b"{}",
                )
            elif kind == "request.approval_decision":
                await _handle_approval_decision(
                    ws=ws,
                    frame=frame,
                    payload=payload,
                    conv=conv,
                    binding_id=binding_id,
                    js=js,
                )
            else:
                await _send_event(
                    ws,
                    {
                        "type": "event.run.error",
                        "code": "PROTOCOL_UNKNOWN_TYPE",
                        "message": f"unknown frame type {kind!r}",
                    },
                )
    except WebSocketDisconnect:
        # 01b Open Question 1 (locked): closing the tab does NOT
        # cancel the active run. The agent container keeps going
        # and the next GET /runs/{id} reports the truth.
        pass
    finally:
        for t in fwd_tasks:
            t.cancel()
        for t in fwd_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        for sub in (stream_sub, outbound_sub, approval_sub):
            with contextlib.suppress(Exception):
                await sub.unsubscribe()


# ---------------------------------------------------------------------------
# Per-frame handlers
# ---------------------------------------------------------------------------


async def _handle_request_run(
    *,
    ws: WebSocket,
    frame: dict[str, Any],
    payload: WsTicketPayload,
    conv: Any,
    binding_id: str,
    js: "JetStreamContext",
    active_run_lock: asyncio.Lock,
) -> str | None:
    """Process a ``request.run`` frame.

    Returns the active run_id (cached or freshly minted) so the
    caller can stamp follow-up events. On any validation failure,
    sends an ``event.run.error`` frame and returns the previous
    ``active_run_id`` (caller should keep using it).
    """
    text = frame.get("input")
    if not isinstance(text, str) or not text:
        await _send_event(
            ws,
            {
                "type": "event.run.error",
                "code": "PROTOCOL_MISSING_INPUT",
                "message": "request.run requires non-empty 'input' string",
            },
        )
        return None
    idempotency_key = frame.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        await _send_event(
            ws,
            {
                "type": "event.run.error",
                "code": "PROTOCOL_MISSING_IDEMPOTENCY_KEY",
                "message": (
                    "request.run requires a non-empty 'idempotency_key'; "
                    "01b mandates client-minted UUID4 per send"
                ),
            },
        )
        return None

    async def _create_run_and_store_msg() -> str:
        """Atomically INSERT runs + INSERT message. See lifecycle docstring."""
        async with active_run_lock, tenant_conn(payload.tenant_id) as conn:
            run_id = await create_run(
                tenant_id=payload.tenant_id,
                conversation_id=conv.id,
                conn=conn,
            )
        sender_id = payload.user_id
        sender_name = "User"
        ts = datetime.now(UTC).isoformat()
        # Single message_id used for BOTH the local store_message call
        # below AND the NATS event below. The orchestrator's
        # _handle_incoming subscriber also stores the message; with
        # different UUIDs the store_message ON CONFLICT clause didn't
        # fire and the user's input ended up duplicated in DB — UI
        # rendered the same line twice. Reusing the id makes the
        # second write a no-op upsert.
        user_msg_id = str(uuid.uuid4())
        await store_message(
            tenant_id=payload.tenant_id,
            conversation_id=conv.id,
            msg_id=user_msg_id,
            sender=sender_id,
            sender_name=sender_name,
            content=text,
            timestamp=ts,
            is_from_me=False,
            run_id=run_id,
        )
        # NATS publish — orchestrator picks up via web.inbound.{binding_id}
        inbound = WebInboundMessage(
            chat_id=conv.channel_chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            timestamp=ts,
            msg_id=user_msg_id,
        )
        try:
            await js.publish(
                f"web.inbound.{binding_id}", inbound.to_bytes()
            )
        except Exception:  # NATS hiccup — log but don't strand the run
            logger.warning(
                "ws_stream: NATS publish failed; run row stays running",
                run_id=run_id,
                exc_info=True,
            )
        return run_id

    run_id, was_cached = await idempotency_cache.lookup_or_remember(
        conversation_id=conv.id,
        idempotency_key=idempotency_key,
        run_id_factory_async=_create_run_and_store_msg,
    )
    await _send_event(
        ws,
        {
            "type": "event.run.started",
            "run_id": run_id,
            "idempotent": was_cached,
        },
    )
    return run_id


async def _handle_approval_decision(
    *,
    ws: WebSocket,
    frame: dict[str, Any],
    payload: WsTicketPayload,
    conv: Any,
    binding_id: str,
    js: JetStreamContext,
) -> None:
    """Relay a ✅/❌ on a HITL approval to the orchestrator.

    IDOR posture (locked, §10 S4): the browser supplies only ``request_id`` +
    ``decision``. The approver identity is stamped here from the *verified
    ticket* (``payload.user_id`` / ``payload.tenant_id``) — never from the
    frame — and the subject ``web.approval_decision.{binding_id}.{chat_id}``
    carries authenticated ids the orchestrator re-derives a tenant/conversation
    guard from. A compromised browser can at most replay an id it already owns;
    it cannot forge *who* approved or reach another conversation's request.
    """
    request_id = frame.get("request_id")
    decision = frame.get("decision")
    if not isinstance(request_id, str) or not request_id:
        await _send_event(
            ws,
            {
                "type": "event.run.error",
                "code": "PROTOCOL_MISSING_REQUEST_ID",
                "message": "request.approval_decision requires 'request_id'",
            },
        )
        return
    if decision not in ("approve", "reject"):
        await _send_event(
            ws,
            {
                "type": "event.run.error",
                "code": "PROTOCOL_BAD_DECISION",
                "message": "decision must be 'approve' or 'reject'",
            },
        )
        return
    note = frame.get("note")
    body = {
        "request_id": request_id,
        "decision": decision,
        "note": note if isinstance(note, str) else None,
        # Authenticated, server-stamped from the ticket — not the browser.
        "decided_by": payload.user_id,
        "tenant_id": payload.tenant_id,
        "conversation_id": conv.id,
    }
    with contextlib.suppress(Exception):
        await js.publish(
            f"web.approval_decision.{binding_id}.{conv.channel_chat_id}",
            json.dumps(body).encode(),
        )


async def _handle_request_cancel(
    *,
    ws: WebSocket,
    frame: dict[str, Any],
    payload: WsTicketPayload,
) -> None:
    """Forward a cancel request to the orchestrator via NATS.

    Symmetric with the REST endpoint :mod:`webui.v1.runs` — both
    publish ``web.run.cancel.{run_id}`` and let the orchestrator
    do the actual ``status='cancelled'`` UPDATE. The WS handler
    never writes the terminal status itself (would create a ghost).
    """
    run_id = frame.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        await _send_event(
            ws,
            {
                "type": "event.run.error",
                "code": "PROTOCOL_MISSING_RUN_ID",
                "message": "request.cancel requires 'run_id'",
            },
        )
        return
    # We don't load the run row here to check ``terminal`` — the
    # orchestrator can no-op a terminal cancel via the lifecycle
    # helper's ``WHERE status='running'`` gate. Loading would
    # double the DB cost on a hot path.
    await publish_run_cancel(
        run_id=run_id,
        tenant_id=payload.tenant_id,
        conversation_id=payload.conversation_id,
    )


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def register_routes(app: Any) -> None:
    """Attach the WS endpoint to a FastAPI app.

    Called from :mod:`webui.main` so the route lands on the same
    app the rest of /api/v1 lives on. We don't use an
    :class:`APIRouter` because FastAPI's router-level
    ``add_api_websocket_route`` doesn't gracefully compose path
    parameters across includes in every version we run against;
    mounting directly avoids the surprise.
    """
    app.add_api_websocket_route(
        "/api/v1/conversations/{conversation_id}/stream",
        stream,
    )
