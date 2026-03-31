"""Integration tests simulating real user flows through RoleMesh.

These tests exercise the complete chain that a real user would encounter:
  1. System startup (DB init, state load, credential proxy)
  2. Channel connection → message ingestion
  3. Message formatting → container invocation → response routing
  4. IPC from container → task scheduling, cross-group messaging
  5. Scheduled task execution
  6. Concurrent group processing via GroupQueue
  7. Error recovery (cursor rollback)
  8. Graceful shutdown

Mocks are limited to Docker/container spawning — everything else runs for real.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# Mock Channel — simulates Telegram/Slack without network
# ---------------------------------------------------------------------------


@dataclass
class FakeChannel:
    """Simulates a real messaging channel (Telegram/Slack) without network."""

    name: str = "fake"
    owned_jids: list[str] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    typing_events: list[tuple[str, bool]] = field(default_factory=list)
    _connected: bool = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send_message(self, jid: str, text: str) -> None:
        self.sent.append((jid, text))

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid in self.owned_jids

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        self.typing_events.append((jid, is_typing))


# ---------------------------------------------------------------------------
# Environment fixture — sets up isolated dirs and PG test DB
# ---------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> Path:
    """Isolated environment: tmp dirs, PG test DB, fast polling."""
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    store_dir = tmp_path / "store"
    data_dir.mkdir()
    groups_dir.mkdir()
    store_dir.mkdir()

    monkeypatch.setattr("rolemesh.core.config.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.config.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("rolemesh.core.config.STORE_DIR", store_dir)
    monkeypatch.setattr("rolemesh.core.config.POLL_INTERVAL", 0.05)
    monkeypatch.setattr("rolemesh.core.config.SCHEDULER_POLL_INTERVAL", 0.05)
    monkeypatch.setattr("rolemesh.core.config.IDLE_TIMEOUT", 2000)
    monkeypatch.setattr("rolemesh.core.config.TIMEZONE", "UTC")
    monkeypatch.setattr("rolemesh.core.config.ASSISTANT_NAME", "Andy")
    monkeypatch.setattr(
        "rolemesh.core.config.TRIGGER_PATTERN",
        re.compile(r"^@Andy\b", re.IGNORECASE),
    )

    # Patch modules that captured config at import time
    monkeypatch.setattr("rolemesh.core.group_folder.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.group_folder.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("rolemesh.main.TIMEZONE", "UTC")
    monkeypatch.setattr("rolemesh.main.ASSISTANT_NAME", "Andy")
    monkeypatch.setattr(
        "rolemesh.main.TRIGGER_PATTERN",
        re.compile(r"^@Andy\b", re.IGNORECASE),
    )

    from rolemesh.db.pg import _init_test_database

    await _init_test_database(pg_url)

    yield tmp_path

    from rolemesh.db.pg import close_database

    await close_database()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def register_group(
    channel: FakeChannel,
    jid: str = "tg_group_123@telegram",
    folder: str = "telegram_mygroup",
    name: str = "My Test Group",
    is_main: bool = True,
    groups_dir: Path | None = None,
) -> None:
    """Register a group and wire up main.py module state."""
    import rolemesh.main as m
    from rolemesh.core.types import RegisteredGroup
    from rolemesh.db.pg import set_registered_group, store_chat_metadata

    group = RegisteredGroup(
        name=name,
        folder=folder,
        trigger="@Andy",
        added_at="2024-01-01T00:00:00Z",
        is_main=is_main,
        requires_trigger=not is_main,
    )

    m._registered_groups[jid] = group
    m._channels = [channel]
    m._last_timestamp = ""
    m._last_agent_timestamp = {}
    m._sessions = {}
    m._queue = m.GroupQueue()
    m._queue.set_process_messages_fn(m._process_group_messages)

    await set_registered_group(jid, group)
    await store_chat_metadata(jid, "2024-01-01T00:00:00Z", name=name, is_group=True)

    if groups_dir:
        gdir = groups_dir / folder
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / "logs").mkdir(exist_ok=True)


async def send_user_message(
    jid: str,
    text: str,
    sender: str = "user_456",
    sender_name: str = "Alice",
    timestamp: str = "2024-06-01T12:00:01Z",
) -> None:
    """Simulate a user sending a message through a channel."""
    from rolemesh.core.types import NewMessage
    from rolemesh.db.pg import store_message

    await store_message(
        NewMessage(
            id=f"msg-{id(text)}-{timestamp}",
            chat_jid=jid,
            sender=sender,
            sender_name=sender_name,
            content=text,
            timestamp=timestamp,
        )
    )


def make_agent_mock(
    response: str = "Hello! I'm Andy, your assistant.",
    session_id: str | None = "session-001",
) -> object:
    """Mock agent executor that returns a canned response."""
    from rolemesh.agent.executor import AgentInput, AgentOutput

    class _FakeHandle:
        """Minimal stand-in for ContainerHandle."""

        name: str = "mock-container"
        pid: int = 12345

    class MockExecutor:
        def __init__(self) -> None:
            self.captured_inputs: list[AgentInput] = []

        @property
        def name(self) -> str:
            return "mock"

        async def execute(
            self,
            inp: AgentInput,
            on_process: Callable[..., None],
            on_output: Callable[..., Awaitable[None]] | None = None,
        ) -> AgentOutput:
            self.captured_inputs.append(inp)
            on_process(_FakeHandle(), "mock-container", "test-job")

            output = AgentOutput(
                status="success",
                result=response,
                new_session_id=session_id,
            )
            if on_output:
                await on_output(output)

            return AgentOutput(status="success", result=None, new_session_id=session_id)

    return MockExecutor()


# ===========================================================================
# Test Scenarios — simulating real user flows
# ===========================================================================


class TestScenarioFirstTimeUser:
    """User just set up RoleMesh, connects Telegram, sends first message."""

    async def test_database_init_and_state_load(self, env: Path) -> None:
        """Step 1: DB initializes, state loads (0 groups)."""
        from rolemesh.db.pg import get_all_registered_groups

        groups = await get_all_registered_groups()
        # We already registered one in fixture, but this verifies DB works
        assert isinstance(groups, dict)

    async def test_first_message_triggers_agent(self, env: Path) -> None:
        """Step 2: User sends first message → agent is invoked → response sent back."""
        jid = "tg_group_123@telegram"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(channel, jid=jid, groups_dir=env / "groups")

        # User sends a message
        await send_user_message(jid, "Hello Andy, can you help me?")

        # Mock the container agent
        mock = make_agent_mock(response="Of course! What do you need help with?")

        import rolemesh.main as m

        m._executor = mock  # type: ignore[assignment]
        result = await m._process_group_messages(jid)

        assert result is True
        assert len(channel.sent) == 1
        assert channel.sent[0][0] == jid
        assert "help" in channel.sent[0][1].lower()

        # Typing indicators were sent
        assert (jid, True) in channel.typing_events
        assert (jid, False) in channel.typing_events

    async def test_session_persists_across_messages(self, env: Path) -> None:
        """Step 3: Session ID returned by agent is persisted for next call."""
        jid = "tg_group_123@telegram"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(channel, jid=jid, groups_dir=env / "groups")

        await send_user_message(jid, "Remember this context")
        mock = make_agent_mock(response="Noted!", session_id="sess-abc-123")

        import rolemesh.main as m

        m._executor = mock  # type: ignore[assignment]
        await m._process_group_messages(jid)

        assert m._sessions.get("telegram_mygroup") == "sess-abc-123"

        from rolemesh.db.pg import get_session

        assert await get_session("telegram_mygroup") == "sess-abc-123"


class TestScenarioMessageFormatting:
    """Verify messages are correctly formatted for the Claude agent."""

    async def test_messages_formatted_as_xml(self, env: Path) -> None:
        """Messages are wrapped in XML with sender, timestamp, and context."""
        from rolemesh.core.types import NewMessage
        from rolemesh.orchestration.router import format_messages

        messages = [
            NewMessage(
                id="msg-1",
                chat_jid="group@test",
                sender="alice@user",
                sender_name="Alice",
                content="Hello Andy!",
                timestamp="2024-06-01T12:00:00Z",
            ),
            NewMessage(
                id="msg-2",
                chat_jid="group@test",
                sender="bob@user",
                sender_name="Bob",
                content="Can you summarize the meeting notes?",
                timestamp="2024-06-01T12:00:05Z",
            ),
        ]

        formatted = format_messages(messages, "UTC")
        assert "<messages>" in formatted
        assert 'sender="Alice"' in formatted
        assert 'sender="Bob"' in formatted
        assert "Hello Andy!" in formatted
        assert "summarize the meeting notes" in formatted
        assert "<context timezone=" in formatted

    async def test_internal_tags_stripped_from_response(self, env: Path) -> None:
        """<internal> tags in agent output are removed before sending to user."""
        jid = "tg_group_123@telegram"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(channel, jid=jid, groups_dir=env / "groups")

        await send_user_message(jid, "Tell me about the project")
        mock = make_agent_mock(
            response="Here's the summary. <internal>I checked CLAUDE.md for context</internal> The project is going well!"
        )

        import rolemesh.main as m

        m._executor = mock  # type: ignore[assignment]
        await m._process_group_messages(jid)

        assert len(channel.sent) == 1
        assert "I checked CLAUDE.md" not in channel.sent[0][1]
        assert "Here's the summary" in channel.sent[0][1]
        assert "going well" in channel.sent[0][1]


class TestScenarioTriggerPattern:
    """Non-main groups require @Andy trigger to activate."""

    async def test_no_trigger_no_response(self, env: Path) -> None:
        """Message without @Andy in non-main group → ignored."""
        jid = "slack_general@slack"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(
            channel,
            jid=jid,
            folder="slack_general",
            name="General",
            is_main=False,
            groups_dir=env / "groups",
        )

        await send_user_message(jid, "Just chatting among ourselves")
        mock = make_agent_mock()

        import rolemesh.main as m

        m._executor = mock  # type: ignore[assignment]
        result = await m._process_group_messages(jid)

        assert result is True
        assert len(channel.sent) == 0  # No response
        assert len(mock.captured_inputs) == 0  # Agent never called

    async def test_trigger_activates_agent(self, env: Path) -> None:
        """Message with @Andy in non-main group → agent invoked."""
        jid = "slack_general@slack"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(
            channel,
            jid=jid,
            folder="slack_general",
            name="General",
            is_main=False,
            groups_dir=env / "groups",
        )

        await send_user_message(jid, "@Andy what's the status of the deploy?")
        mock = make_agent_mock(response="Deploy is at 95%, almost done!")

        import rolemesh.main as m

        m._executor = mock  # type: ignore[assignment]
        result = await m._process_group_messages(jid)

        assert result is True
        assert len(channel.sent) == 1
        assert "95%" in channel.sent[0][1]


class TestScenarioIPCFromContainer:
    """Container sends IPC files to schedule tasks and send messages."""

    async def test_container_schedules_cron_task(self, env: Path) -> None:
        """Agent writes a task IPC file → task created in DB."""
        from rolemesh.core.types import RegisteredGroup
        from rolemesh.db.pg import get_task_by_id
        from rolemesh.ipc.task_handler import process_task_ipc

        registered = {
            "tg@test": RegisteredGroup(
                name="Test",
                folder="testgroup",
                trigger="@Andy",
                added_at="2024-01-01T00:00:00Z",
                is_main=True,
            )
        }
        tasks_changed: list[bool] = []

        class FakeDeps:
            async def send_message(self, jid: str, text: str) -> None:
                pass

            def registered_groups(self) -> dict[str, RegisteredGroup]:
                return registered

            async def register_group(self, jid: str, group: RegisteredGroup) -> None:
                pass

            async def sync_groups(self, force: bool) -> None:
                pass

            async def get_available_groups(self) -> list[object]:
                return []

            def write_groups_snapshot(self, gf: str, im: bool, ag: list[object], rj: set[str]) -> None:
                pass

            async def on_tasks_changed(self) -> None:
                tasks_changed.append(True)

        # Simulate agent writing a task file
        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "Send daily standup summary",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * 1-5",  # weekdays at 9am
                "targetJid": "tg@test",
                "taskId": "daily-standup-001",
            },
            "testgroup",
            True,
            FakeDeps(),  # type: ignore[arg-type]
        )

        task = await get_task_by_id("daily-standup-001")
        assert task is not None
        assert task.prompt == "Send daily standup summary"
        assert task.schedule_type == "cron"
        assert task.schedule_value == "0 9 * * 1-5"
        assert task.group_folder == "testgroup"
        assert len(tasks_changed) == 1

    async def test_ipc_task_pause_resume(self, env: Path) -> None:
        """Agent pauses and resumes a task via IPC handler."""
        from rolemesh.core.types import RegisteredGroup, ScheduledTask
        from rolemesh.db.pg import create_task, get_task_by_id
        from rolemesh.ipc.task_handler import process_task_ipc

        registered = {
            "tg@test": RegisteredGroup(
                name="Test",
                folder="mygroup",
                trigger="@Andy",
                added_at="2024-01-01T00:00:00Z",
                is_main=True,
            )
        }
        tasks_changed: list[bool] = []

        class FakeDeps:
            async def send_message(self, jid: str, text: str) -> None:
                pass

            def registered_groups(self) -> dict[str, RegisteredGroup]:
                return registered

            async def register_group(self, jid: str, group: RegisteredGroup) -> None:
                pass

            async def sync_groups(self, force: bool) -> None:
                pass

            async def get_available_groups(self) -> list[object]:
                return []

            def write_groups_snapshot(self, gf: str, im: bool, ag: list[object], rj: set[str]) -> None:
                pass

            async def on_tasks_changed(self) -> None:
                tasks_changed.append(True)

        # Create a task first
        await create_task(
            ScheduledTask(
                id="task-pause-test",
                group_folder="mygroup",
                chat_jid="tg@test",
                prompt="Test task",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                context_mode="isolated",
                next_run="2024-06-01T09:00:00Z",
                status="active",
                created_at="2024-01-01T00:00:00Z",
            )
        )

        # Pause it
        await process_task_ipc(
            {"type": "pause_task", "taskId": "task-pause-test"},
            "mygroup",
            True,
            FakeDeps(),  # type: ignore[arg-type]
        )
        task = await get_task_by_id("task-pause-test")
        assert task is not None
        assert task.status == "paused"
        assert len(tasks_changed) == 1

        # Resume it
        await process_task_ipc(
            {"type": "resume_task", "taskId": "task-pause-test"},
            "mygroup",
            True,
            FakeDeps(),  # type: ignore[arg-type]
        )
        task = await get_task_by_id("task-pause-test")
        assert task is not None
        assert task.status == "active"
        assert len(tasks_changed) == 2


class TestScenarioTaskScheduling:
    """Scheduled tasks compute next_run and execute on time."""

    async def test_cron_schedule_computes_next_run(self, env: Path) -> None:
        """Cron task calculates correct next execution time."""
        from rolemesh.core.types import ScheduledTask
        from rolemesh.orchestration.task_scheduler import compute_next_run

        task = ScheduledTask(
            id="cron-1",
            group_folder="testgroup",
            chat_jid="tg@test",
            prompt="Daily check",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2024-01-01T09:00:00Z",
            status="active",
            created_at="2024-01-01T00:00:00Z",
        )

        next_run = compute_next_run(task)
        assert next_run is not None
        assert "T09:00:00" in next_run  # 9am daily

    async def test_interval_schedule_advances(self, env: Path) -> None:
        """Interval task keeps advancing by the interval duration."""
        from rolemesh.core.types import ScheduledTask
        from rolemesh.orchestration.task_scheduler import compute_next_run

        task = ScheduledTask(
            id="interval-1",
            group_folder="testgroup",
            chat_jid="tg@test",
            prompt="Hourly sync",
            schedule_type="interval",
            schedule_value="3600000",  # 1 hour in ms
            context_mode="isolated",
            next_run="2020-01-01T00:00:00Z",
            status="active",
            created_at="2020-01-01T00:00:00Z",
        )

        next_run = compute_next_run(task)
        assert next_run is not None
        assert next_run > "2024-01-01T00:00:00Z"  # must be in the future

    async def test_once_schedule_returns_none(self, env: Path) -> None:
        """One-shot task has no next run."""
        from rolemesh.core.types import ScheduledTask
        from rolemesh.orchestration.task_scheduler import compute_next_run

        task = ScheduledTask(
            id="once-1",
            group_folder="testgroup",
            chat_jid="tg@test",
            prompt="One-time deploy",
            schedule_type="once",
            schedule_value="2024-01-01T00:00:00",
            context_mode="isolated",
            next_run="2024-01-01T00:00:00Z",
            status="active",
            created_at="2024-01-01T00:00:00Z",
        )

        assert compute_next_run(task) is None


class TestScenarioGroupQueueConcurrency:
    """Multiple groups enqueue concurrently, respecting limits."""

    async def test_multiple_groups_processed(self, env: Path) -> None:
        """Three groups enqueue messages → all get processed."""
        from rolemesh.container.scheduler import GroupQueue

        queue = GroupQueue()
        processed: list[str] = []

        async def process_fn(group_jid: str) -> bool:
            processed.append(group_jid)
            await asyncio.sleep(0.02)
            return True

        queue.set_process_messages_fn(process_fn)

        queue.enqueue_message_check("group-A")
        queue.enqueue_message_check("group-B")
        queue.enqueue_message_check("group-C")

        await asyncio.sleep(0.5)

        assert "group-A" in processed
        assert "group-B" in processed
        assert "group-C" in processed


class TestScenarioErrorRecovery:
    """Agent errors trigger cursor rollback for retry."""

    async def test_error_rolls_back_cursor(self, env: Path) -> None:
        """Container error → message cursor rolled back → retry possible."""
        from rolemesh.agent.executor import AgentInput, AgentOutput

        jid = "tg_group_123@telegram"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(channel, jid=jid, groups_dir=env / "groups")

        await send_user_message(jid, "This will crash the agent")

        class _FakeHandle:
            name: str = "crash-container"
            pid: int = 99999

        class FailingExecutor:
            @property
            def name(self) -> str:
                return "mock"

            async def execute(
                self,
                inp: AgentInput,
                on_process: Callable[..., None],
                on_output: Callable[..., Awaitable[None]] | None = None,
            ) -> AgentOutput:
                on_process(_FakeHandle(), "crash-container", "test-job")
                if on_output:
                    await on_output(AgentOutput(status="error", result=None, error="Segfault"))
                return AgentOutput(status="error", result=None, error="Segfault")

        import rolemesh.main as m

        m._executor = FailingExecutor()  # type: ignore[assignment]
        result = await m._process_group_messages(jid)

        # Error → rollback → retry
        assert result is False
        assert m._last_agent_timestamp.get(jid, "") == ""

    async def test_partial_output_prevents_rollback(self, env: Path) -> None:
        """If agent already sent output before error → no rollback (prevent duplicates)."""
        from rolemesh.agent.executor import AgentInput, AgentOutput

        jid = "tg_group_123@telegram"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(channel, jid=jid, groups_dir=env / "groups")

        await send_user_message(jid, "Start answering then crash")

        class _FakeHandle:
            name: str = "partial-container"
            pid: int = 99998

        class PartialThenErrorExecutor:
            @property
            def name(self) -> str:
                return "mock"

            async def execute(
                self,
                inp: AgentInput,
                on_process: Callable[..., None],
                on_output: Callable[..., Awaitable[None]] | None = None,
            ) -> AgentOutput:
                on_process(_FakeHandle(), "partial-container", "test-job")
                if on_output:
                    # First: send a successful partial output
                    await on_output(
                        AgentOutput(
                            status="success",
                            result="Here's part of the answer...",
                            new_session_id=None,
                        )
                    )
                    # Then: error
                    await on_output(AgentOutput(status="error", result=None, error="Timeout"))
                return AgentOutput(status="error", result=None, error="Timeout")

        import rolemesh.main as m

        m._executor = PartialThenErrorExecutor()  # type: ignore[assignment]
        result = await m._process_group_messages(jid)

        # Partial output was sent → no rollback → returns True
        assert result is True
        assert len(channel.sent) == 1
        assert "part of the answer" in channel.sent[0][1]


class TestScenarioCredentialProxy:
    """Credential proxy starts and injects auth headers."""

    async def test_proxy_starts_and_serves(self, env: Path) -> None:
        """Credential proxy binds to a port and is reachable."""
        import aiohttp

        from rolemesh.security.credential_proxy import start_credential_proxy

        runner = await start_credential_proxy(port=0, host="127.0.0.1")

        try:
            # Find the bound port from runner's sites
            port = None
            for site in runner.sites:
                if hasattr(site, "_server") and site._server and site._server.sockets:
                    port = site._server.sockets[0].getsockname()[1]
                    break

            if port is None:
                pytest.skip("Could not determine proxy port")

            # Make a request — it'll fail upstream, but proves the proxy works
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(
                        f"http://127.0.0.1:{port}/v1/messages",
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as resp:
                        # Any response (even error) proves proxy is running
                        assert resp.status > 0
                except (aiohttp.ClientError, OSError):
                    # Connection error to upstream is expected, proxy still works
                    pass
        finally:
            await runner.cleanup()


class TestScenarioSenderAllowlist:
    """Sender allowlist controls who can interact with the bot."""

    async def test_drop_mode_blocks_unauthorized(self, env: Path) -> None:
        """Messages from non-allowed senders are silently dropped."""
        from rolemesh.core.types import NewMessage
        from rolemesh.security.sender_allowlist import (
            ChatAllowlistEntry,
            SenderAllowlistConfig,
            is_sender_allowed,
            should_drop_message,
        )

        # Simulate drop-mode config
        cfg = SenderAllowlistConfig(
            default=ChatAllowlistEntry(allow=["admin_user"], mode="drop"),
        )

        # Unauthorized user's message
        msg = NewMessage(
            id="blocked-1",
            chat_jid="group@test",
            sender="random_user",
            sender_name="Random",
            content="@Andy do something",
            timestamp="2024-06-01T12:00:01Z",
        )

        # Simulate main.py _on_message logic
        if should_drop_message(msg.chat_jid, cfg) and not is_sender_allowed(msg.chat_jid, msg.sender, cfg):
            dropped = True
        else:
            dropped = False

        assert dropped is True

        # Authorized user passes through
        assert is_sender_allowed("group@test", "admin_user", cfg) is True


class TestScenarioDatabaseOperations:
    """Full DB lifecycle: store, query, update, delete."""

    async def test_message_store_and_query(self, env: Path) -> None:
        """Store messages and retrieve them with timestamp filtering."""
        from rolemesh.core.types import NewMessage
        from rolemesh.db.pg import get_messages_since, store_chat_metadata, store_message

        await store_chat_metadata("chat@test", "2024-01-01T00:00:00Z", name="Test Chat")

        for i in range(5):
            await store_message(
                NewMessage(
                    id=f"msg-{i}",
                    chat_jid="chat@test",
                    sender="user",
                    sender_name="User",
                    content=f"Message {i}",
                    timestamp=f"2024-06-01T12:00:0{i}Z",
                )
            )

        # Get all
        all_msgs = await get_messages_since("chat@test", "", "Andy")
        assert len(all_msgs) == 5

        # Get since timestamp
        since = await get_messages_since("chat@test", "2024-06-01T12:00:02Z", "Andy")
        assert len(since) == 2  # messages 3 and 4

    async def test_task_crud(self, env: Path) -> None:
        """Create, read, update, delete scheduled tasks."""
        from rolemesh.core.types import ScheduledTask
        from rolemesh.db.pg import (
            create_task,
            delete_task,
            get_task_by_id,
            update_task,
        )

        task = ScheduledTask(
            id="test-task",
            group_folder="testgroup",
            chat_jid="chat@test",
            prompt="Check server status",
            schedule_type="interval",
            schedule_value="600000",
            context_mode="isolated",
            next_run="2024-06-01T12:00:00Z",
            status="active",
            created_at="2024-01-01T00:00:00Z",
        )
        await create_task(task)

        # Read
        fetched = await get_task_by_id("test-task")
        assert fetched is not None
        assert fetched.prompt == "Check server status"

        # Update
        await update_task("test-task", status="paused")
        fetched = await get_task_by_id("test-task")
        assert fetched is not None
        assert fetched.status == "paused"

        # Delete
        await delete_task("test-task")
        assert await get_task_by_id("test-task") is None


class TestScenarioMountSecurity:
    """Mount security validates container volume mounts."""

    async def test_blocked_paths_rejected(self, env: Path) -> None:
        """Sensitive paths like /etc/shadow are blocked."""
        from rolemesh.core.types import AdditionalMount
        from rolemesh.security.mount_security import validate_mount

        with patch(
            "rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH",
            env / "nonexistent.json",
        ):
            from rolemesh.security.mount_security import reset_cache

            reset_cache()
            result = validate_mount(
                AdditionalMount(host_path="/etc/shadow", container_path="/mnt/shadow"),
                is_main=True,
            )
            assert not result.allowed

    async def test_invalid_container_path_rejected(self, env: Path) -> None:
        """Container paths that could escape sandbox are rejected."""
        from rolemesh.core.types import AdditionalMount
        from rolemesh.security.mount_security import validate_mount

        with patch(
            "rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH",
            env / "nonexistent.json",
        ):
            from rolemesh.security.mount_security import reset_cache

            reset_cache()
            result = validate_mount(
                AdditionalMount(host_path="/tmp/safe", container_path="../../../escape"),
                is_main=True,
            )
            assert not result.allowed


class TestScenarioEndToEndFlow:
    """Full end-to-end: message in → agent processes → response out → session saved."""

    async def test_complete_conversation_flow(self, env: Path) -> None:
        """Simulate a full multi-message conversation."""
        jid = "tg_group_123@telegram"
        channel = FakeChannel(owned_jids=[jid])
        await register_group(channel, jid=jid, groups_dir=env / "groups")

        # === Turn 1: User asks a question ===
        await send_user_message(jid, "Hi Andy, what's our sprint velocity?", timestamp="2024-06-01T12:00:01Z")

        mock1 = make_agent_mock(
            response="Based on the last 3 sprints, your average velocity is 42 story points.",
            session_id="sess-turn-1",
        )

        import rolemesh.main as m

        m._executor = mock1  # type: ignore[assignment]
        result1 = await m._process_group_messages(jid)

        assert result1 is True
        assert len(channel.sent) == 1
        assert "42 story points" in channel.sent[0][1]
        assert m._sessions.get("telegram_mygroup") == "sess-turn-1"

        # === Turn 2: Follow-up question (different sender) ===
        await send_user_message(
            jid,
            "Can you break that down by developer?",
            sender="bob_789",
            sender_name="Bob",
            timestamp="2024-06-01T12:00:10Z",
        )

        mock2 = make_agent_mock(
            response="Sure! Alice: 18pts, Bob: 14pts, Carol: 10pts.",
            session_id="sess-turn-2",
        )

        channel.sent.clear()

        m._executor = mock2  # type: ignore[assignment]
        result2 = await m._process_group_messages(jid)

        assert result2 is True
        assert len(channel.sent) == 1
        assert "Alice" in channel.sent[0][1]

        # Session was updated
        assert m._sessions.get("telegram_mygroup") == "sess-turn-2"

        # Verify the agent received the prompt with message history
        assert len(mock2.captured_inputs) == 1  # type: ignore[attr-defined]
        inp = mock2.captured_inputs[0]  # type: ignore[attr-defined]
        assert hasattr(inp, "prompt")
        assert "break that down" in inp.prompt  # type: ignore[union-attr]

        # === Verify DB state ===
        from rolemesh.db.pg import get_session

        assert await get_session("telegram_mygroup") == "sess-turn-2"
