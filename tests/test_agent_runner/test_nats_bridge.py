"""Integration tests for the NATS bridge (agent_runner/main.py).

Uses a REAL NATS server (from docker-compose.dev.yml) and a FakeBackend
that simulates agent behavior without Claude SDK or Pi. This tests all
6 NATS channels end-to-end:

  1. KV init (agent-init bucket)
  2. Results stream (agent.{id}.results)
  3. Follow-up input (agent.{id}.input)
  4. Shutdown signal (agent.{id}.shutdown, request-reply)
  5. Messages (agent.{id}.messages, via send_message tool)
  6. Tasks (agent.{id}.tasks, via schedule_task tool)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import nats
import pytest
from nats.js.api import StreamConfig

from agent_runner.backend import BackendEvent, ErrorEvent, ResultEvent, SessionInitEvent
from agent_runner.main import (
    ContainerOutput,
    drain_nats_input,
    publish_output,
    run_query_loop,
)
from agent_runner.tools.context import ToolContext
from rolemesh.ipc.protocol import AgentInitData

NATS_URL = "nats://localhost:4222"


# ---------------------------------------------------------------------------
# FakeBackend — controllable agent simulator
# ---------------------------------------------------------------------------


class FakeBackend:
    """A test backend that lets the test script control when results are emitted."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.follow_ups: list[str] = []
        self.aborted = False
        self.started = False
        self.shutdown_called = False
        self._listener: Callable[[BackendEvent], Awaitable[None]] | None = None
        self._prompt_done: asyncio.Event = asyncio.Event()
        self._session_id: str | None = None
        # Control: set this to make run_prompt return
        self.prompt_result: str = "fake response"
        # Control: how long run_prompt takes (simulate agent work)
        self.prompt_delay: float = 0.1

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def subscribe(self, listener: Any) -> None:
        self._listener = listener

    async def _emit(self, event: BackendEvent) -> None:
        if self._listener:
            await self._listener(event)

    async def start(self, init: Any, tool_ctx: Any, mcp_servers: Any = None) -> None:
        self.started = True
        self._session_id = "fake-session-1"
        await self._emit(SessionInitEvent(session_id=self._session_id))

    async def run_prompt(self, text: str) -> None:
        self.prompts.append(text)
        await asyncio.sleep(self.prompt_delay)
        if not self.aborted:
            await self._emit(ResultEvent(text=self.prompt_result, new_session_id=self._session_id))

    async def handle_follow_up(self, text: str) -> None:
        self.follow_ups.append(text)

    async def abort(self) -> None:
        self.aborted = True

    async def shutdown(self) -> None:
        self.shutdown_called = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def nats_conn():
    """Connect to NATS and ensure the agent-ipc stream exists."""
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    # Ensure stream exists (idempotent) with the current subject list, which
    # must include agent.*.interrupt so the bridge's JS consumer can bind.
    cfg = StreamConfig(
        name="agent-ipc",
        subjects=[
            "agent.*.results",
            "agent.*.input",
            "agent.*.interrupt",
            "agent.*.messages",
            "agent.*.tasks",
        ],
    )
    try:
        await js.add_stream(cfg)
    except Exception:
        # Stream already exists with a stale subject list — update in place
        # so the new agent.*.interrupt subject gets routed.
        await js.update_stream(cfg)
    # Ensure KV bucket exists
    try:
        await js.create_key_value(bucket="agent-init")
    except Exception:
        pass
    yield nc, js
    await nc.close()


def _unique_job_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _make_init(job_id: str, **overrides: Any) -> AgentInitData:
    defaults = dict(
        prompt="hello agent",
        group_folder="test-group",
        chat_jid="test-chat",
        permissions={"task_schedule": True},
        tenant_id="test-tenant",
        coworker_id="test-cw",
        conversation_id="test-conv",
    )
    defaults.update(overrides)
    return AgentInitData(**defaults)


# ---------------------------------------------------------------------------
# Channel 1: KV init — write and read AgentInitData
# ---------------------------------------------------------------------------


class TestChannel1KVInit:
    async def test_write_and_read_init_data(self, nats_conn: tuple) -> None:
        """AgentInitData serialized to KV can be deserialized back."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id, prompt="test prompt", session_id="s-123")

        kv = await js.key_value("agent-init")
        await kv.put(job_id, init.serialize())

        entry = await kv.get(job_id)
        restored = AgentInitData.deserialize(entry.value)
        assert restored.prompt == "test prompt"
        assert restored.session_id == "s-123"
        assert restored.tenant_id == "test-tenant"
        assert restored.permissions == {"task_schedule": True}

        await kv.delete(job_id)


# ---------------------------------------------------------------------------
# Channel 2: Results stream — publish_output and subscribe
# ---------------------------------------------------------------------------


class TestChannel2Results:
    async def test_publish_output_received_by_subscriber(self, nats_conn: tuple) -> None:
        """publish_output writes to JetStream; a subscriber receives it."""
        nc, js = nats_conn
        job_id = _unique_job_id()

        sub = await js.subscribe(f"agent.{job_id}.results")

        await publish_output(
            js, job_id,
            ContainerOutput(status="success", result="answer", new_session_id="sid-1"),
        )

        msg = await asyncio.wait_for(sub.next_msg(timeout=2), timeout=3)
        await msg.ack()
        data = json.loads(msg.data)
        assert data["status"] == "success"
        assert data["result"] == "answer"
        assert data["newSessionId"] == "sid-1"

        await sub.unsubscribe()

    async def test_error_output_format(self, nats_conn: tuple) -> None:
        nc, js = nats_conn
        job_id = _unique_job_id()

        sub = await js.subscribe(f"agent.{job_id}.results")

        await publish_output(
            js, job_id,
            ContainerOutput(status="error", result=None, error="timeout"),
        )

        msg = await asyncio.wait_for(sub.next_msg(timeout=2), timeout=3)
        await msg.ack()
        data = json.loads(msg.data)
        assert data["status"] == "error"
        assert data["error"] == "timeout"
        assert data["result"] is None

        await sub.unsubscribe()


# ---------------------------------------------------------------------------
# Channel 3: Follow-up input — messages injected during query
# ---------------------------------------------------------------------------


class TestChannel3FollowUpInput:
    async def test_drain_nats_input_collects_pending(self, nats_conn: tuple) -> None:
        """drain_nats_input reads all pending messages from the input subject."""
        nc, js = nats_conn
        job_id = _unique_job_id()

        # Publish 3 pending messages
        for i in range(3):
            await js.publish(
                f"agent.{job_id}.input",
                json.dumps({"type": "message", "text": f"pending-{i}"}).encode(),
            )
        await asyncio.sleep(0.1)

        sub = await js.subscribe(f"agent.{job_id}.input")
        messages = await drain_nats_input(sub)
        await sub.unsubscribe()
        assert messages == ["pending-0", "pending-1", "pending-2"]

    async def test_drain_empty_returns_empty(self, nats_conn: tuple) -> None:
        nc, js = nats_conn
        job_id = _unique_job_id()
        sub = await js.subscribe(f"agent.{job_id}.input")
        messages = await drain_nats_input(sub)
        await sub.unsubscribe()
        assert messages == []

    async def test_follow_up_during_query_reaches_backend(self, nats_conn: tuple) -> None:
        """A message published to agent.{id}.input during run_prompt
        is received by the bridge and forwarded to backend.handle_follow_up."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id)

        backend = FakeBackend()
        backend.prompt_delay = 0.5  # Keep query alive long enough

        # Write init to KV (not used by run_query_loop directly, but needed for consistency)
        kv = await js.key_value("agent-init")
        await kv.put(job_id, init.serialize())

        import agent_runner.main as bridge_mod
        original_create = bridge_mod._create_backend

        def mock_create(name: str) -> FakeBackend:
            return backend

        bridge_mod._create_backend = mock_create
        try:
            loop_task = asyncio.create_task(run_query_loop(init, nc, js, job_id))

            # Wait for backend to start processing
            await asyncio.sleep(0.15)

            # Send follow-up message
            await js.publish(
                f"agent.{job_id}.input",
                json.dumps({"type": "message", "text": "follow-up question"}).encode(),
            )
            await asyncio.sleep(0.2)

            # Send close signal to exit the loop
            reply = await nc.request(f"agent.{job_id}.shutdown", b"", timeout=2)
            assert reply.data == b"ack"

            await asyncio.wait_for(loop_task, timeout=5)

            assert "follow-up question" in backend.follow_ups
        finally:
            bridge_mod._create_backend = original_create
            await kv.delete(job_id)


# ---------------------------------------------------------------------------
# Channel 4: Close signal — request-reply termination
# ---------------------------------------------------------------------------


class TestChannel4CloseSignal:
    async def test_close_during_idle_exits_loop(self, nats_conn: tuple) -> None:
        """Close signal sent while bridge waits for next message → loop exits."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id)

        backend = FakeBackend()
        backend.prompt_delay = 0.05  # Quick first prompt

        import agent_runner.main as bridge_mod
        original_create = bridge_mod._create_backend

        bridge_mod._create_backend = lambda _: backend
        try:
            loop_task = asyncio.create_task(run_query_loop(init, nc, js, job_id))

            # Wait for first prompt to finish and bridge to enter idle wait
            await asyncio.sleep(0.3)

            # Send close signal
            reply = await nc.request(f"agent.{job_id}.shutdown", b"", timeout=2)
            assert reply.data == b"ack"

            await asyncio.wait_for(loop_task, timeout=5)

            assert backend.prompts == ["hello agent"]
            assert backend.shutdown_called
        finally:
            bridge_mod._create_backend = original_create

    async def test_close_during_query_aborts_backend(self, nats_conn: tuple) -> None:
        """Close signal sent while run_prompt is active → backend.abort() called."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id)

        backend = FakeBackend()
        backend.prompt_delay = 2.0  # Long-running query

        import agent_runner.main as bridge_mod
        original_create = bridge_mod._create_backend

        bridge_mod._create_backend = lambda _: backend
        try:
            loop_task = asyncio.create_task(run_query_loop(init, nc, js, job_id))

            # Wait for query to start
            await asyncio.sleep(0.2)

            # Send close during query
            reply = await nc.request(f"agent.{job_id}.shutdown", b"", timeout=2)
            assert reply.data == b"ack"

            await asyncio.wait_for(loop_task, timeout=5)

            assert backend.aborted
            assert backend.shutdown_called
        finally:
            bridge_mod._create_backend = original_create


# ---------------------------------------------------------------------------
# Channel 5 & 6: Messages and Tasks — tool publishes through real NATS
# ---------------------------------------------------------------------------


class TestChannel5And6ToolPublishes:
    async def test_send_message_tool_publishes_to_nats(self, nats_conn: tuple) -> None:
        """send_message tool publishes to agent.{id}.messages via real NATS."""
        nc, js = nats_conn
        job_id = _unique_job_id()

        sub = await js.subscribe(f"agent.{job_id}.messages")

        ctx = ToolContext(
            js=js,
            job_id=job_id,
            chat_jid="chat-1",
            group_folder="grp",
            permissions={},
            tenant_id="t-1",
            coworker_id="cw-1",
            conversation_id="conv-1",
        )

        from agent_runner.tools.rolemesh_tools import send_message
        await send_message({"text": "hello from tool"}, ctx)
        await asyncio.sleep(0.2)

        msg = await asyncio.wait_for(sub.next_msg(timeout=2), timeout=3)
        await msg.ack()
        data = json.loads(msg.data)
        assert data["type"] == "message"
        assert data["text"] == "hello from tool"
        assert data["chatJid"] == "chat-1"

        await sub.unsubscribe()

    async def test_schedule_task_tool_publishes_to_nats(self, nats_conn: tuple) -> None:
        """schedule_task tool publishes to agent.{id}.tasks via real NATS."""
        nc, js = nats_conn
        job_id = _unique_job_id()

        sub = await js.subscribe(f"agent.{job_id}.tasks")

        ctx = ToolContext(
            js=js,
            job_id=job_id,
            chat_jid="chat-1",
            group_folder="grp",
            permissions={"task_schedule": True},
            tenant_id="t-1",
            coworker_id="cw-1",
            conversation_id="conv-1",
        )

        from agent_runner.tools.rolemesh_tools import schedule_task
        result = await schedule_task(
            {"prompt": "daily check", "schedule_type": "cron", "schedule_value": "0 9 * * *"},
            ctx,
        )
        await asyncio.sleep(0.2)

        assert "isError" not in result

        msg = await asyncio.wait_for(sub.next_msg(timeout=2), timeout=3)
        await msg.ack()
        data = json.loads(msg.data)
        assert data["type"] == "schedule_task"
        assert data["prompt"] == "daily check"
        assert data["schedule_type"] == "cron"
        assert data["tenantId"] == "t-1"

        await sub.unsubscribe()


# ---------------------------------------------------------------------------
# Full bridge integration: prompt → result → session update cycle
# ---------------------------------------------------------------------------


class TestBridgeFullCycle:
    async def test_prompt_produces_result_on_nats(self, nats_conn: tuple) -> None:
        """Full cycle: init → prompt → ResultEvent → published to results subject."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id)

        backend = FakeBackend()
        backend.prompt_result = "the answer is 42"
        backend.prompt_delay = 0.05

        # Subscribe to results before starting
        sub = await js.subscribe(f"agent.{job_id}.results")

        import agent_runner.main as bridge_mod
        original_create = bridge_mod._create_backend

        bridge_mod._create_backend = lambda _: backend
        try:
            loop_task = asyncio.create_task(run_query_loop(init, nc, js, job_id))

            # Collect results
            results: list[dict] = []
            for _ in range(3):  # Expect: result + session update
                try:
                    msg = await asyncio.wait_for(sub.next_msg(timeout=2), timeout=3)
                    await msg.ack()
                    results.append(json.loads(msg.data))
                except Exception:
                    break

            # Close to exit
            await nc.request(f"agent.{job_id}.shutdown", b"", timeout=2)
            await asyncio.wait_for(loop_task, timeout=5)

            # Verify result was published
            result_msgs = [r for r in results if r.get("result") is not None]
            assert any(r["result"] == "the answer is 42" for r in result_msgs)
            assert all(r["status"] == "success" for r in results)
        finally:
            bridge_mod._create_backend = original_create
            await sub.unsubscribe()

    async def test_scheduled_task_prefix_added(self, nats_conn: tuple) -> None:
        """When is_scheduled_task=True, prompt gets the SCHEDULED TASK prefix."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id, is_scheduled_task=True, prompt="run backup")

        backend = FakeBackend()
        backend.prompt_delay = 0.05

        import agent_runner.main as bridge_mod
        original_create = bridge_mod._create_backend

        bridge_mod._create_backend = lambda _: backend
        try:
            loop_task = asyncio.create_task(run_query_loop(init, nc, js, job_id))
            await asyncio.sleep(0.3)
            await nc.request(f"agent.{job_id}.shutdown", b"", timeout=2)
            await asyncio.wait_for(loop_task, timeout=5)

            assert len(backend.prompts) >= 1
            assert "[SCHEDULED TASK" in backend.prompts[0]
            assert "run backup" in backend.prompts[0]
        finally:
            bridge_mod._create_backend = original_create

    async def test_second_prompt_after_follow_up_message(self, nats_conn: tuple) -> None:
        """After first query ends, a new NATS input message triggers a second prompt."""
        nc, js = nats_conn
        job_id = _unique_job_id()
        init = _make_init(job_id, prompt="first question")

        backend = FakeBackend()
        backend.prompt_delay = 0.05

        import agent_runner.main as bridge_mod
        original_create = bridge_mod._create_backend

        bridge_mod._create_backend = lambda _: backend
        try:
            loop_task = asyncio.create_task(run_query_loop(init, nc, js, job_id))

            # Wait for first query to finish
            await asyncio.sleep(0.3)

            # Send second message (between queries, not during)
            await js.publish(
                f"agent.{job_id}.input",
                json.dumps({"type": "message", "text": "second question"}).encode(),
            )
            await asyncio.sleep(0.3)

            # Close
            await nc.request(f"agent.{job_id}.shutdown", b"", timeout=2)
            await asyncio.wait_for(loop_task, timeout=5)

            assert backend.prompts == ["first question", "second question"]
        finally:
            bridge_mod._create_backend = original_create
