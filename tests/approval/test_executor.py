"""Unit tests for ApprovalWorker message handling.

We drive _handle_message directly rather than running the JetStream
loop — that gives us deterministic control over the decided payload
and lets us assert against the DB + fake HTTP proxy without a NATS
container. The worker's _execute_actions path is intentionally
exercised via aioresponses-style stub against a local aiohttp.Session.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import aiohttp
import pytest
from aiohttp import web

from rolemesh.approval.executor import ApprovalWorker
from rolemesh.db.pg import (
    create_approval_policy,
    create_approval_request,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    get_approval_request,
    list_approval_audit,
)

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_to_conversation(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))


@dataclass
class _FakeMsg:
    subject: str
    data: bytes
    acks: list[int] = field(default_factory=list)

    async def ack(self) -> None:
        self.acks.append(1)


async def _seed_request(
    *, status: str = "approved", actions: list[dict[str, Any]] | None = None
) -> tuple[str, str, str, str, str]:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
    u = await create_user(tenant_id=t.id, name="O", email="o@x.com", role="owner")
    cw = await create_coworker(
        tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    c = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    p = await create_approval_policy(
        tenant_id=t.id,
        coworker_id=cw.id,
        mcp_server_name="erp",
        tool_name="refund",
        condition_expr={"always": True},
        approver_user_ids=[u.id],
    )
    act = actions or [
        {"mcp_server": "erp", "tool_name": "refund", "params": {"amount": 100}}
    ]
    req = await create_approval_request(
        tenant_id=t.id,
        coworker_id=cw.id,
        conversation_id=c.id,
        policy_id=p.id,
        user_id=u.id,
        job_id="job-exec",
        mcp_server_name="erp",
        actions=act,
        action_hashes=[f"hash-{i}" for i in range(len(act))],
        rationale="r",
        source="proposal",
        status=status,
        resolved_approvers=[u.id],
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    return req.id, c.id, u.id, cw.id, t.id


# ---------------------------------------------------------------------------
# Fake credential-proxy server (aiohttp test app)
# ---------------------------------------------------------------------------


class _ProxyRecorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        # server_name -> response (dict with status_code, body_json) or None (default 200)
        self.server_override: dict[str, dict[str, Any]] = {}

    async def handle(self, request: web.Request) -> web.Response:
        server_name = request.match_info["server"]
        body = await request.json()
        hdrs = dict(request.headers)
        self.requests.append((server_name, hdrs.get("X-Idempotency-Key", ""), body))
        override = self.server_override.get(server_name)
        if override:
            return web.json_response(override.get("body", {}), status=override.get("status", 200))
        return web.json_response({"ok": True, "server": server_name}, status=200)


@pytest.fixture
async def proxy_base():
    recorder = _ProxyRecorder()
    app = web.Application()
    app.router.add_post("/mcp-proxy/{server}/", recorder.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    host, port = site._server.sockets[0].getsockname()[:2]
    base = f"http://{host}:{port}"
    yield base, recorder
    await runner.cleanup()


# ---------------------------------------------------------------------------
# Approved execution
# ---------------------------------------------------------------------------


class TestApprovedExecution:
    async def test_execute_success_transitions_to_executed(self, proxy_base) -> None:
        base, recorder = proxy_base
        req_id, conv_id, _user_id, _cw, _t = await _seed_request(status="approved")
        ch = _FakeChannel()
        w = ApprovalWorker(js=None, channel_sender=ch, proxy_base_url=base)  # type: ignore[arg-type]
        msg = _FakeMsg(
            subject=f"approval.decided.{req_id}",
            data=json.dumps({"status": "approved"}).encode(),
        )
        await w._handle_message(msg)

        req = await get_approval_request(req_id)
        assert req is not None and req.status == "executed"
        audit_actions = [e.action for e in await list_approval_audit(req_id)]
        assert audit_actions[-2:] == ["executing", "executed"]
        # Report went to the origin conversation.
        assert any(conv_id == c for c, _t in ch.sent)
        # Credential proxy was called with idempotency + user id
        assert len(recorder.requests) == 1
        server, idk, body = recorder.requests[0]
        assert server == "erp"
        assert idk == "hash-0"
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "refund"
        assert msg.acks == [1]

    async def test_partial_failure_marks_execution_failed(self, proxy_base) -> None:
        base, recorder = proxy_base
        # Two actions; force the second server to 500.
        recorder.server_override["erp2"] = {"status": 500, "body": {"err": "boom"}}
        req_id, _conv, _user, _cw, _t = await _seed_request(
            status="approved",
            actions=[
                {"mcp_server": "erp", "tool_name": "a", "params": {}},
                {"mcp_server": "erp2", "tool_name": "b", "params": {}},
            ],
        )
        ch = _FakeChannel()
        w = ApprovalWorker(js=None, channel_sender=ch, proxy_base_url=base)  # type: ignore[arg-type]
        msg = _FakeMsg(
            subject=f"approval.decided.{req_id}",
            data=json.dumps({"status": "approved"}).encode(),
        )
        await w._handle_message(msg)

        req = await get_approval_request(req_id)
        assert req is not None and req.status == "execution_failed"
        audit_actions = [e.action for e in await list_approval_audit(req_id)]
        assert "execution_failed" in audit_actions
        # Both actions were attempted (best-effort batch).
        assert len(recorder.requests) == 2

    async def test_duplicate_decided_does_not_double_execute(self, proxy_base) -> None:
        base, recorder = proxy_base
        req_id, _c, _u, _cw, _t = await _seed_request(status="approved")
        ch = _FakeChannel()
        w = ApprovalWorker(js=None, channel_sender=ch, proxy_base_url=base)  # type: ignore[arg-type]
        msg1 = _FakeMsg(
            subject=f"approval.decided.{req_id}",
            data=json.dumps({"status": "approved"}).encode(),
        )
        msg2 = _FakeMsg(
            subject=f"approval.decided.{req_id}",
            data=json.dumps({"status": "approved"}).encode(),
        )
        await w._handle_message(msg1)
        await w._handle_message(msg2)
        # The first claim consumed the approved status; the second
        # message must drop silently and NOT call the proxy.
        assert len(recorder.requests) == 1


# ---------------------------------------------------------------------------
# Rejection notification
# ---------------------------------------------------------------------------


class TestRejectionNotify:
    async def test_rejected_sends_notification_no_execute(self, proxy_base) -> None:
        base, recorder = proxy_base
        # Seed as already-rejected (engine would have set it before publish)
        req_id, conv_id, _user, _cw, _t = await _seed_request(status="rejected")
        ch = _FakeChannel()
        w = ApprovalWorker(js=None, channel_sender=ch, proxy_base_url=base)  # type: ignore[arg-type]
        msg = _FakeMsg(
            subject=f"approval.decided.{req_id}",
            data=json.dumps({"status": "rejected", "note": "no"}).encode(),
        )
        await w._handle_message(msg)
        assert len(recorder.requests) == 0
        assert any(conv_id == c and "rejected" in t for c, t in ch.sent)

    async def test_rejected_without_conversation_id_does_not_raise(
        self, proxy_base
    ) -> None:
        base, _recorder = proxy_base
        # Seed a row without conversation_id.
        t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:8]}")
        u = await create_user(tenant_id=t.id, name="O", email="o@x.com", role="owner")
        cw = await create_coworker(
            tenant_id=t.id, name="CW", folder=f"cw-{uuid.uuid4().hex[:8]}"
        )
        p = await create_approval_policy(
            tenant_id=t.id,
            coworker_id=cw.id,
            mcp_server_name="erp",
            tool_name="refund",
            condition_expr={"always": True},
            approver_user_ids=[u.id],
        )
        req = await create_approval_request(
            tenant_id=t.id,
            coworker_id=cw.id,
            conversation_id=None,
            policy_id=p.id,
            user_id=u.id,
            job_id="j-no-conv",
            mcp_server_name="erp",
            actions=[{"mcp_server": "erp", "tool_name": "t", "params": {}}],
            action_hashes=["h"],
            rationale=None,
            source="proposal",
            status="rejected",
            resolved_approvers=[u.id],
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        ch = _FakeChannel()
        w = ApprovalWorker(js=None, channel_sender=ch, proxy_base_url=base)  # type: ignore[arg-type]
        msg = _FakeMsg(
            subject=f"approval.decided.{req.id}",
            data=json.dumps({"status": "rejected"}).encode(),
        )
        await w._handle_message(msg)  # must not raise
        assert ch.sent == []


# Silence unused-import warnings.
_ = aiohttp
_ = urlparse
