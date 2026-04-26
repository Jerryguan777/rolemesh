"""End-to-end harness for approval-module integration tests.

Boots real infrastructure:
  - real Postgres (via ``test_db`` fixture in parent conftest.py)
  - real NATS JetStream (reuses docker-compose.dev.yml on localhost:4222)
  - real credential proxy (aiohttp, random port)
  - real ApprovalEngine + ApprovalWorker + maintenance loop
  - real FastAPI admin router

Mocks ONLY at the system boundary:
  - MCP server (programmable response queue)
  - Channel gateway (SinkGateway captures send_message calls)

Per CLAUDE.md §"测试理念" — mocking internal modules hides cross-process
bugs. We only stub external dependencies we have no other way to
control.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import nats
from aiohttp import web
from fastapi import FastAPI
from nats.js.api import KeyValueConfig, StreamConfig

from rolemesh.approval.engine import ApprovalEngine
from rolemesh.approval.executor import ApprovalWorker
from rolemesh.approval.expiry import run_approval_maintenance_loop
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.auth.provider import AuthenticatedUser
from rolemesh.db import pg
from rolemesh.security.credential_proxy import (
    register_mcp_server,
    start_credential_proxy,
)
from webui import admin as admin_module
from webui.dependencies import (
    get_current_user,
    require_manage_agents,
    require_manage_tenant,
    require_manage_users,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from nats.aio.client import Client
    from nats.js.client import JetStreamContext


# ---------------------------------------------------------------------------
# Mock MCP server
# ---------------------------------------------------------------------------


@dataclass
class MCPRequest:
    """A single received JSON-RPC call."""

    body: dict[str, Any]
    headers: dict[str, str]


class MockMCPServer:
    """Programmable MCP server for E2E tests.

    The server records every incoming JSON-RPC call and returns responses
    from a queue. The queue is per-tool so tests can set distinct
    responses for different actions in the same batch.
    """

    def __init__(self) -> None:
        self.received: list[MCPRequest] = []
        # Queue of responses; each is either a dict (returned as 200
        # JSON) or a (status, body) tuple for non-2xx returns.
        self._responses: list[dict[str, Any] | tuple[int, Any]] = []
        self.default_response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
        self.delay_seconds: float = 0.0
        self._runner: web.AppRunner | None = None
        self.base_url: str = ""

    def enqueue(self, response: dict[str, Any] | tuple[int, Any]) -> None:
        self._responses.append(response)

    def reset(self) -> None:
        self.received.clear()
        self._responses.clear()
        self.delay_seconds = 0.0

    async def _handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.received.append(
            MCPRequest(body=body, headers=dict(request.headers))
        )
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self._responses:
            resp = self._responses.pop(0)
            if isinstance(resp, tuple):
                status, body_data = resp
                if isinstance(body_data, dict):
                    return web.json_response(body_data, status=status)
                return web.Response(text=str(body_data), status=status)
            return web.json_response(resp)
        return web.json_response(self.default_response)

    async def start(self) -> str:
        """Start the server on a random port; returns the base URL."""
        app = web.Application()
        # The credential proxy strips `/mcp-proxy/<name>/` and forwards
        # the remainder. It expects the upstream to handle a relative
        # path; our mock accepts anything under root.
        app.router.add_post("/", self._handle)
        app.router.add_post("/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]
        assert sockets
        port = sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


# ---------------------------------------------------------------------------
# Sink gateway (captures channel messages)
# ---------------------------------------------------------------------------


@dataclass
class SinkMessage:
    conversation_id: str
    text: str


class SinkChannelSender:
    """Implements the ChannelSender protocol by capturing everything."""

    def __init__(self) -> None:
        self.messages: list[SinkMessage] = []

    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        self.messages.append(
            SinkMessage(conversation_id=conversation_id, text=text)
        )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class SeedResult:
    tenant_id: str
    coworker_id: str
    owner_user_id: str
    conversation_id: str
    binding_id: str


@dataclass
class OrchestratorHarness:
    """Wired-up orchestrator-side components for an E2E run.

    Holds references so tests can drive the flow directly:
      - ``nc`` / ``js``    — raw NATS client + JetStream for publishing
                             agent-side messages or inspecting streams
      - ``engine``         — ApprovalEngine used by both the NATS-taps
                             and the REST admin router
      - ``worker``         — ApprovalWorker subscribed to approval.decided.*
      - ``mcp``            — MockMCPServer (already registered with proxy)
      - ``channel``        — SinkChannelSender (captures notifications)
      - ``admin_app``      — FastAPI app with admin router mounted
      - ``task_sub``       — JetStream subscription that routes
                             agent-tasks messages into the engine via
                             process_task_ipc (mirrors main._handle_tasks)
      - ``cancel_sub``     — subscription on approval.cancel_for_job.*

    Tests use ``publish_agent_task(job_id, payload)`` to simulate a
    container publishing, and ``api()`` to get a pre-authorised httpx
    AsyncClient for REST calls.
    """

    nc: Client
    js: JetStreamContext
    engine: ApprovalEngine
    worker: ApprovalWorker
    mcp: MockMCPServer
    channel: SinkChannelSender
    admin_app: FastAPI
    task_sub: Any
    cancel_sub: Any
    proxy_runner: Any
    maintenance_stop: asyncio.Event
    maintenance_task: asyncio.Task[None]
    mcp_server_name: str
    _subscribe_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def publish_agent_task(self, job_id: str, payload: dict[str, Any]) -> None:
        """Publish a task on the agent IPC subject the container would use."""
        await self.js.publish(
            f"agent.{job_id}.tasks", json.dumps(payload).encode()
        )

    async def publish_cancel(self, job_id: str) -> None:
        await self.js.publish(f"approval.cancel_for_job.{job_id}", b"")

    def api_client(self, user: AuthenticatedUser) -> httpx.AsyncClient:
        """Return an httpx AsyncClient whose dependencies are overridden
        to authenticate as the given user. Caller must close."""
        # Clone the admin app and install overrides just for this client
        # so concurrent clients don't clobber each other's auth state.
        app = FastAPI()
        app.include_router(admin_module.router)

        async def _authed() -> AuthenticatedUser:
            return user

        app.dependency_overrides[get_current_user] = _authed
        app.dependency_overrides[require_manage_agents] = _authed
        app.dependency_overrides[require_manage_tenant] = _authed
        app.dependency_overrides[require_manage_users] = _authed
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def wait_for(
        self,
        cond: Callable[[], Awaitable[bool]],
        *,
        timeout: float = 5.0,
        interval: float = 0.05,
    ) -> None:
        """Poll until ``cond()`` returns True, or raise AssertionError."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            if await cond():
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise AssertionError(
                    f"Condition not satisfied within {timeout}s"
                )
            await asyncio.sleep(interval)


async def seed_tenant(
    *,
    name_prefix: str = "T",
    owner_role: str = "owner",
    with_web_binding: bool = False,
) -> SeedResult:
    t = await pg.create_tenant(
        name=name_prefix, slug=f"{name_prefix.lower()}-{uuid.uuid4().hex[:8]}"
    )
    owner = await pg.create_user(
        tenant_id=t.id, name=f"{name_prefix}-owner",
        email=f"{name_prefix.lower()}@x.com", role=owner_role,
    )
    cw = await pg.create_coworker(
        tenant_id=t.id,
        name=f"{name_prefix}-cw",
        folder=f"cw-{uuid.uuid4().hex[:8]}",
    )
    b = await pg.create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type=("web" if with_web_binding else "telegram"),
        credentials={"bot_token": "x"},
    )
    conv = await pg.create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=str(uuid.uuid4()),
    )
    return SeedResult(
        tenant_id=t.id,
        coworker_id=cw.id,
        owner_user_id=owner.id,
        conversation_id=conv.id,
        binding_id=b.id,
    )


def make_auth_user(
    *, tenant_id: str, user_id: str, role: str = "owner",
    email: str = "x@x.com", name: str = "X",
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=user_id, tenant_id=tenant_id, role=role,
        email=email, name=name,
    )


@asynccontextmanager
async def orchestrator_harness(
    nats_url: str,
    *,
    mcp_server_name: str = "test-mcp",
) -> AsyncIterator[OrchestratorHarness]:
    """Boot a full orchestrator stack for an E2E test.

    Exclusive ownership of NATS streams/consumers by this test: we use a
    uniquely-named durable consumer per run and tear it down at exit.
    """
    # --- NATS ---
    nc = await nats.connect(nats_url)
    js = nc.jetstream()
    # Idempotent stream + KV bucket setup (matches NatsTransport.connect)
    for cfg in (
        StreamConfig(
            name="agent-ipc",
            subjects=[
                "agent.*.results",
                "agent.*.input",
                "agent.*.interrupt",
                "agent.*.messages",
                "agent.*.tasks",
            ],
        ),
        StreamConfig(
            name="approval-ipc",
            subjects=["approval.decided.*", "approval.cancel_for_job.*"],
        ),
    ):
        try:
            await js.add_stream(cfg)
        except Exception:
            await js.update_stream(cfg)
    with suppress(Exception):
        await js.create_key_value(config=KeyValueConfig(bucket="agent-init"))

    # --- Mock MCP + credential proxy ---
    mcp = MockMCPServer()
    await mcp.start()
    # Pick an ephemeral port for the credential proxy to avoid clashing
    # with any long-running dev instance.
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        proxy_port = s.getsockname()[1]
    proxy_runner = await start_credential_proxy(proxy_port, "127.0.0.1")
    # Register the mock MCP server under a test-specific name.
    register_mcp_server(
        mcp_server_name,
        mcp.base_url,
        headers={},
        auth_mode="service",
    )

    # --- Engine, worker, maintenance ---
    channel = SinkChannelSender()

    async def _no_convs(user_id: str, coworker_id: str) -> list[str]:
        return []

    async def _get_conv(conv_id: str) -> object | None:
        # Mirror main.py: notification path needs an unscoped lookup
        # because the ChannelSender protocol carries no tenant context.
        return await pg.get_conversation_for_notification(conv_id)

    resolver = NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_no_convs,
        get_conversation=_get_conv,
        webui_base_url=None,
    )
    engine = ApprovalEngine(
        publisher=js, channel_sender=channel, resolver=resolver
    )
    admin_module.set_approval_engine(engine)
    worker = ApprovalWorker(
        js=js,
        channel_sender=channel,
        proxy_base_url=f"http://127.0.0.1:{proxy_port}",
    )
    await worker.start()

    maintenance_stop = asyncio.Event()
    maintenance_task = asyncio.create_task(
        run_approval_maintenance_loop(
            engine,
            interval_seconds=0.1,  # fast for tests
            stop_event=maintenance_stop,
        )
    )

    # --- Subscriptions that mirror main._handle_tasks / _on_cancel_for_job ---
    task_sub = await js.subscribe(
        "agent.*.tasks", durable=f"e2e-tasks-{uuid.uuid4().hex[:8]}"
    )
    cancel_sub = await js.subscribe(
        "approval.cancel_for_job.*",
        durable=f"e2e-cancel-{uuid.uuid4().hex[:8]}",
    )

    async def _task_loop() -> None:
        async for msg in task_sub.messages:
            try:
                data = json.loads(msg.data)
                # Mirror main._handle_tasks: look up coworker, override
                # tenant_id with the trusted value. Production resolves
                # via in-memory _state.coworkers; the e2e harness has no
                # such state, so we hit the DB directly using the raw
                # pool (this is a system-level dispatch lookup, not a
                # tenant-scoped business call).
                claimed_cw = data.get("coworkerId", "")
                source_tenant: str | None = None
                source_id: str | None = None
                if claimed_cw:
                    pool = pg._get_pool()
                    async with pool.acquire() as conn:
                        row = await conn.fetchrow(
                            "SELECT id, tenant_id FROM coworkers WHERE id = $1::uuid",
                            claimed_cw,
                        )
                    if row is not None:
                        source_tenant = str(row["tenant_id"])
                        source_id = str(row["id"])
                if source_tenant is not None and source_id is not None:
                    tenant_id = source_tenant
                    coworker_id = source_id
                else:
                    tenant_id = data.get("tenantId", "")
                    coworker_id = claimed_cw
                task_type = data.get("type")
                if task_type == "submit_proposal":
                    await engine.handle_proposal(
                        data, tenant_id=tenant_id, coworker_id=coworker_id
                    )
                elif task_type == "auto_approval_request":
                    await engine.handle_auto_intercept(
                        data, tenant_id=tenant_id, coworker_id=coworker_id
                    )
                await msg.ack()
            except Exception:
                with suppress(Exception):
                    await msg.ack()

    async def _cancel_loop() -> None:
        async for msg in cancel_sub.messages:
            try:
                jid = msg.subject.rsplit(".", 1)[-1]
                await engine.cancel_for_job(jid)
                await msg.ack()
            except Exception:
                with suppress(Exception):
                    await msg.ack()

    task_loop_task = asyncio.create_task(_task_loop())
    cancel_loop_task = asyncio.create_task(_cancel_loop())

    admin_app = FastAPI()
    admin_app.include_router(admin_module.router)

    harness = OrchestratorHarness(
        nc=nc,
        js=js,
        engine=engine,
        worker=worker,
        mcp=mcp,
        channel=channel,
        admin_app=admin_app,
        task_sub=task_sub,
        cancel_sub=cancel_sub,
        proxy_runner=proxy_runner,
        maintenance_stop=maintenance_stop,
        maintenance_task=maintenance_task,
        mcp_server_name=mcp_server_name,
        _subscribe_tasks=[task_loop_task, cancel_loop_task],
    )

    try:
        yield harness
    finally:
        # Tear down in reverse order.
        for t in harness._subscribe_tasks:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t
        with suppress(Exception):
            await task_sub.unsubscribe()
        with suppress(Exception):
            await cancel_sub.unsubscribe()
        maintenance_stop.set()
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task
        await worker.stop()
        admin_module.set_approval_engine(None)
        await mcp.stop()
        with suppress(Exception):
            await proxy_runner.cleanup()
        await nc.close()


# Silence unused warnings
_ = urlparse
