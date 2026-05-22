"""Unit + integration tests for the 03a PR2 approval surface on
the v1 WS handler.

Two scopes:

* The ``_handle_request_approval`` callback — tested directly with
  a fake WS so the per-frame behaviour (INV-7 translation, engine
  dispatch, error frames) is pinned without standing up the full
  JetStream/Postgres stack.
* The engine NATS publish surface — covered against the real
  testcontainer Postgres + a fake publisher to confirm subject
  shapes for the dual fan-out (``conv`` + ``req``).

INV-7 grep is enforced in ``test_inv7_no_wire_strings_in_engine``
to catch regressions where a refactor sneaks a wire literal back
into engine.py / executor.py.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rolemesh.approval.engine import (
    ApprovalEngine,
    ConflictError,
    ForbiddenError,
)
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.auth.bootstrap_actor import (
    BOOTSTRAP_USER_LITERAL,
    BootstrapActorError,
)
from rolemesh.auth.ws_ticket import WsTicketPayload
from rolemesh.db import (
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    list_approval_requests,
)
from webui.v1 import ws_stream
from webui.v1.approval_engine_registry import set_approval_engine

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePublisher:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.publishes.append((subject, data))


class _FakeChannel:
    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        return


def _resolver() -> NotificationTargetResolver:
    async def _convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    async def _conv(conv_id: str) -> object | None:
        return object()

    return NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_convs,
        get_conversation=_conv,
        webui_base_url=None,
    )


def _engine(publisher: _FakePublisher | None = None) -> tuple[
    ApprovalEngine, _FakePublisher
]:
    pub = publisher or _FakePublisher()
    return (
        ApprovalEngine(
            publisher=pub,
            channel_sender=_FakeChannel(),
            resolver=_resolver(),
        ),
        pub,
    )


class _FakeWebSocket:
    """Mimics the surface ``_handle_request_approval`` touches:
    ``client_state == CONNECTED`` + ``send_json`` capture.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        # _send_event guards on this — must be CONNECTED to actually
        # send.
        from starlette.websockets import WebSocketState

        self.client_state = WebSocketState.CONNECTED

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)


def _payload(tenant_id: str, user_id: str, conv_id: str) -> WsTicketPayload:
    # WsTicketPayload is a dataclass-ish container; just instantiate
    # with the fields the handler reads (tenant_id, user_id,
    # conversation_id).
    return WsTicketPayload(
        tenant_id=tenant_id,
        user_id=user_id,
        conversation_id=conv_id,
        exp=int(
            (datetime.now(UTC) + timedelta(seconds=60)).timestamp()
        ),
    )


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


async def _seed() -> tuple[str, str, str, str]:
    t = await create_tenant(
        name="T-wsapp", slug=f"wsapp-{uuid.uuid4().hex[:8]}"
    )
    u = await create_user(
        tenant_id=t.id, name="alice",
        email=f"a-{uuid.uuid4().hex[:6]}@x.com", role="owner",
    )
    cw = await create_coworker(
        tenant_id=t.id, name="cw",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    b = await create_channel_binding(
        coworker_id=cw.id, tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    conv = await create_conversation(
        tenant_id=t.id, coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    return t.id, u.id, cw.id, conv.id


async def _make_pending(
    tenant_id: str,
    user_id: str,
    cw_id: str,
    conv_id: str,
    *,
    approvers: list[str] | None = None,
) -> str:
    p = await create_approval_policy(
        tenant_id=tenant_id, coworker_id=cw_id,
        mcp_server_name="erp", tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=approvers or [user_id],
    )
    req = await create_approval_request(
        tenant_id=tenant_id, coworker_id=cw_id,
        conversation_id=conv_id, policy_id=p.id,
        user_id=user_id, job_id=f"job-{uuid.uuid4().hex[:6]}",
        mcp_server_name="erp",
        actions=[
            {"mcp_server": "erp", "tool_name": "refund",
             "params": {"amount": 500}}
        ],
        action_hashes=[f"h-{uuid.uuid4().hex[:6]}"],
        rationale="r", source="proposal", status="pending",
        resolved_approvers=approvers or [user_id],
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    return req.id


# ---------------------------------------------------------------------------
# Engine WS-event publishes (03a PR2)
# ---------------------------------------------------------------------------


class TestEngineWebPublishes:
    async def test_handle_decision_dual_publishes_resolved(self) -> None:
        """``handle_decision`` must dual-publish ``web.approval.resolved``
        on both ``conv.<id>`` and ``req.<id>`` so chat-WS subscribers
        and queue-page subscribers both wake.
        """
        tenant_id, user_id, cw_id, conv_id = await _seed()
        req_id = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        engine, pub = _engine()
        await engine.handle_decision(
            request_id=req_id,
            tenant_id=tenant_id,
            outcome="approved",
            user_id=user_id,
            note="ok",
        )
        subjects = [s for s, _ in pub.publishes]
        assert f"web.approval.resolved.conv.{conv_id}" in subjects
        assert f"web.approval.resolved.req.{req_id}" in subjects
        # Bodies on both subjects should carry the WS wire decision
        # (``"approve"``), not the engine outcome.
        for s, d in pub.publishes:
            if s.startswith("web.approval.resolved."):
                body = json.loads(d)
                assert body["decision"] == "approve"
                assert body["approval_id"] == req_id
                assert body["actor_user_id"] == user_id

    async def test_reject_translates_to_deny_on_wire(self) -> None:
        """Engine outcome ``rejected`` must surface as wire ``deny``
        (the design's vocabulary mismatch — HTTP uses ``reject`` but
        WS uses ``deny`` — collapses in
        :func:`outcome_to_ws_decision`).
        """
        tenant_id, user_id, cw_id, conv_id = await _seed()
        req_id = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        engine, pub = _engine()
        await engine.handle_decision(
            request_id=req_id,
            tenant_id=tenant_id,
            outcome="rejected",
            user_id=user_id,
        )
        resolved = [
            (s, d) for s, d in pub.publishes
            if s.startswith("web.approval.resolved.")
        ]
        assert resolved, "engine must publish web.approval.resolved on reject"
        for _s, d in resolved:
            body = json.loads(d)
            assert body["decision"] == "deny"

    async def test_safety_path_publishes_required(self) -> None:
        """``create_from_safety`` is the third creation entrypoint
        (alongside proposal + auto_intercept); it too must surface a
        ``web.approval.required`` so the SPA wakes.
        """
        tenant_id, user_id, cw_id, conv_id = await _seed()
        engine, pub = _engine()
        req = await engine.create_from_safety(
            tenant_id=tenant_id, coworker_id=cw_id,
            conversation_id=conv_id, job_id="job-x",
            user_id=user_id, tool_name="bash",
            tool_input={"cmd": "rm -rf /"},
            mcp_server_name="shell",
        )
        assert req is not None
        required = [
            (s, d) for s, d in pub.publishes
            if s.startswith("web.approval.required.")
        ]
        assert required, (
            "safety path must publish web.approval.required for the SPA"
        )
        assert required[0][0] == f"web.approval.required.{conv_id}"

    async def test_required_payload_contains_summary(self) -> None:
        """The forwarder relies on ``summary.tool_name`` /
        ``summary.args`` to render the inline card without a
        follow-up REST call. Pin the payload shape.
        """
        tenant_id, user_id, cw_id, conv_id = await _seed()
        await _make_pending(tenant_id, user_id, cw_id, conv_id)
        # Re-invoke the engine through its public surface so the
        # publish path runs. We use handle_proposal which goes via
        # the builder.
        engine, pub = _engine()
        await engine.handle_proposal(
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                "conversationId": conv_id,
                "jobId": "job-pp",
                "userId": user_id,
                "rationale": "test",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 500},
                    }
                ],
            },
            tenant_id=tenant_id, coworker_id=cw_id,
        )
        required = [
            (s, d) for s, d in pub.publishes
            if s.startswith("web.approval.required.")
        ]
        assert required, "engine must publish web.approval.required"
        body = json.loads(required[0][1])
        assert body["summary"]["tool_name"] == "refund"
        assert body["summary"]["args"] == {"amount": 500}
        assert body["summary"]["mcp_server_name"] == "erp"
        assert body["conversation_id"] == conv_id

    async def test_required_skipped_when_no_conversation(self) -> None:
        """Approval requests with NULL conversation_id (e.g. proposals
        from a coworker without a chat channel) have nowhere to push
        to — the engine must silently skip the WS publish rather
        than crash.
        """
        tenant_id, user_id, cw_id, _conv_id = await _seed()
        engine, pub = _engine()
        await engine.handle_proposal(
            {
                "tenantId": tenant_id,
                "coworkerId": cw_id,
                # No conversationId.
                "jobId": "job-no-conv",
                "userId": user_id,
                "rationale": "headless",
                "actions": [
                    {
                        "mcp_server": "erp",
                        "tool_name": "refund",
                        "params": {"amount": 500},
                    }
                ],
            },
            tenant_id=tenant_id, coworker_id=cw_id,
        )
        required = [
            s for s, _ in pub.publishes
            if s.startswith("web.approval.required.")
        ]
        assert required == []
        # DB row still created — only the WS publish is skipped.
        reqs = await list_approval_requests(tenant_id)
        assert len(reqs) == 1


# ---------------------------------------------------------------------------
# WS request.approval handler — direct dispatch through registered engine
# ---------------------------------------------------------------------------


class _FakeJs:
    """The WS handler signature still threads ``js`` in but the new
    impl doesn't publish; this fake is here to confirm it stays
    unused.
    """

    def __init__(self) -> None:
        self.publishes: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.publishes.append((subject, data))


class TestRequestApprovalHandler:
    async def test_happy_path_invokes_engine_directly(self) -> None:
        """The WS request.approval path must reach the SAME engine
        as the HTTP /decide endpoint — no fan-out via NATS, no
        second implementation of the state machine.
        """
        tenant_id, user_id, cw_id, conv_id = await _seed()
        req_id = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        engine, pub = _engine()
        set_approval_engine(engine)
        try:
            ws = _FakeWebSocket()
            await ws_stream._handle_request_approval(
                ws=ws,  # type: ignore[arg-type]
                frame={
                    "type": "request.approval",
                    "approval_id": req_id,
                    "decision": "approve",
                    "note": "looks fine",
                },
                payload=_payload(tenant_id, user_id, conv_id),
                js=_FakeJs(),  # type: ignore[arg-type]
            )
        finally:
            set_approval_engine(None)
        # No event.run.error was emitted (handler succeeded).
        assert all(f["type"] != "event.run.error" for f in ws.sent), (
            f"unexpected error frames: {ws.sent}"
        )
        # The engine's resolved publish is the canonical "decided"
        # signal — that's what fans out to other WS clients.
        resolved = [
            s for s, _ in pub.publishes
            if s.startswith("web.approval.resolved.")
        ]
        assert resolved, "engine must have published resolved event"

    async def test_unknown_decision_emits_protocol_error(self) -> None:
        engine, _pub = _engine()
        set_approval_engine(engine)
        try:
            ws = _FakeWebSocket()
            tenant_id = str(uuid.uuid4())
            await ws_stream._handle_request_approval(
                ws=ws,  # type: ignore[arg-type]
                frame={
                    "type": "request.approval",
                    "approval_id": str(uuid.uuid4()),
                    "decision": "shrug",
                },
                payload=_payload(tenant_id, str(uuid.uuid4()), str(uuid.uuid4())),
                js=_FakeJs(),  # type: ignore[arg-type]
            )
        finally:
            set_approval_engine(None)
        assert ws.sent
        err = ws.sent[-1]
        assert err["type"] == "event.run.error"
        assert err["code"] == "PROTOCOL_BAD_DECISION"

    async def test_missing_fields_emits_protocol_error(self) -> None:
        engine, _pub = _engine()
        set_approval_engine(engine)
        try:
            ws = _FakeWebSocket()
            await ws_stream._handle_request_approval(
                ws=ws,  # type: ignore[arg-type]
                frame={"type": "request.approval"},
                payload=_payload(
                    str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()),
                ),
                js=_FakeJs(),  # type: ignore[arg-type]
            )
        finally:
            set_approval_engine(None)
        assert ws.sent[-1]["code"] == "PROTOCOL_BAD_APPROVAL"

    async def test_engine_unavailable_emits_envelope(self) -> None:
        set_approval_engine(None)
        ws = _FakeWebSocket()
        await ws_stream._handle_request_approval(
            ws=ws,  # type: ignore[arg-type]
            frame={
                "type": "request.approval",
                "approval_id": str(uuid.uuid4()),
                "decision": "approve",
            },
            payload=_payload(
                str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()),
            ),
            js=_FakeJs(),  # type: ignore[arg-type]
        )
        assert ws.sent[-1]["code"] == "APPROVAL_ENGINE_UNAVAILABLE"

    async def test_forbidden_decider_emits_forbidden_envelope(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed()
        req_id = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        outsider = await create_user(
            tenant_id=tenant_id, name="Eve",
            email=f"e-{uuid.uuid4().hex[:6]}@x.com", role="member",
        )
        engine, _pub = _engine()
        set_approval_engine(engine)
        try:
            ws = _FakeWebSocket()
            await ws_stream._handle_request_approval(
                ws=ws,  # type: ignore[arg-type]
                frame={
                    "type": "request.approval",
                    "approval_id": req_id,
                    "decision": "approve",
                },
                payload=_payload(tenant_id, outsider.id, conv_id),
                js=_FakeJs(),  # type: ignore[arg-type]
            )
        finally:
            set_approval_engine(None)
        assert ws.sent
        err = ws.sent[-1]
        assert err["code"] == "FORBIDDEN"

    async def test_already_decided_emits_conflict_envelope(self) -> None:
        tenant_id, user_id, cw_id, conv_id = await _seed()
        req_id = await _make_pending(tenant_id, user_id, cw_id, conv_id)
        engine, _pub = _engine()
        set_approval_engine(engine)
        try:
            await engine.handle_decision(
                request_id=req_id, tenant_id=tenant_id,
                outcome="approved", user_id=user_id,
            )
            ws = _FakeWebSocket()
            await ws_stream._handle_request_approval(
                ws=ws,  # type: ignore[arg-type]
                frame={
                    "type": "request.approval",
                    "approval_id": req_id,
                    "decision": "deny",
                },
                payload=_payload(tenant_id, user_id, conv_id),
                js=_FakeJs(),  # type: ignore[arg-type]
            )
        finally:
            set_approval_engine(None)
        assert ws.sent[-1]["code"] == "ALREADY_DECIDED"

    async def test_bootstrap_no_owner_surfaces_503_envelope(self) -> None:
        """When the bootstrap fast-path can't resolve a real actor,
        ``BootstrapActorError`` must surface as an event.run.error
        with the helper's structured code — not a generic crash.
        """
        # Build a tenant with NO owner so resolve_actor_user_id
        # raises BootstrapActorError.
        t = await create_tenant(
            name="no-owner",
            slug=f"no-owner-{uuid.uuid4().hex[:8]}",
        )
        member = await create_user(
            tenant_id=t.id, name="m",
            email=f"m-{uuid.uuid4().hex[:6]}@x.com", role="member",
        )
        cw = await create_coworker(
            tenant_id=t.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        b = await create_channel_binding(
            coworker_id=cw.id, tenant_id=t.id,
            channel_type="telegram",
            credentials={"bot_token": "x"},
        )
        c = await create_conversation(
            tenant_id=t.id, coworker_id=cw.id,
            channel_binding_id=b.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        req_id = await _make_pending(
            t.id, member.id, cw.id, c.id, approvers=[member.id],
        )
        engine, _pub = _engine()
        set_approval_engine(engine)
        try:
            ws = _FakeWebSocket()
            await ws_stream._handle_request_approval(
                ws=ws,  # type: ignore[arg-type]
                frame={
                    "type": "request.approval",
                    "approval_id": req_id,
                    "decision": "approve",
                },
                payload=_payload(
                    t.id, BOOTSTRAP_USER_LITERAL, c.id,
                ),
                js=_FakeJs(),  # type: ignore[arg-type]
            )
        finally:
            set_approval_engine(None)
        assert ws.sent
        err = ws.sent[-1]
        assert err["code"] == BootstrapActorError.code


# ---------------------------------------------------------------------------
# INV-7 anti-regression grep
# ---------------------------------------------------------------------------


def test_inv7_no_wire_strings_in_engine() -> None:
    """Engine code must only see ``ApprovalOutcome`` enum values
    (``approved``/``rejected``/``expired``/``cancelled``); any
    reference to the wire strings ``approve``/``deny``/``reject``
    in ``engine.py`` or ``executor.py`` is a translation-boundary
    leak and an INV-7 violation.

    The ``enum_translate`` module is exempt because it owns the
    wire ↔ engine mapping.
    """
    root = (
        Path(__file__).resolve().parent.parent.parent
        / "src/rolemesh/approval"
    )
    pattern = re.compile(
        r"\b(?:'approve'|'deny'|'reject'|\"approve\"|\"deny\"|\"reject\")\b"
    )
    violations: list[tuple[str, int, str]] = []
    for name in ("engine.py", "executor.py"):
        path = root / name
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(line):
                violations.append((str(path), lineno, line.rstrip()))
    assert not violations, (
        "INV-7 grep violation — wire strings should only appear in "
        "enum_translate.py:\n"
        + "\n".join(f"{p}:{n}: {ln}" for p, n, ln in violations)
    )
