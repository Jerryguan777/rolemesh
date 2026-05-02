"""Integration tests simulating real user flows through RoleMesh.

These tests exercise the building blocks of the multi-tenant architecture:
  1. Tenant/Role/Coworker/Conversation CRUD
  2. Message storage and retrieval per conversation
  3. Session management per conversation
  4. Scheduled task lifecycle per coworker
  5. OrchestratorState concurrency control
  6. IPC task handler
  7. Message formatting
  8. Credential proxy startup
  9. Sender allowlist
  10. Mount security
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


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

    monkeypatch.setattr("rolemesh.core.group_folder.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.group_folder.GROUPS_DIR", groups_dir)

    from rolemesh.db import _init_test_database

    await _init_test_database(pg_url)

    yield tmp_path

    from rolemesh.db import close_database

    await close_database()


class TestScenarioFirstTimeUser:
    """User just set up RoleMesh, creates tenant, coworker, and conversations."""

    async def test_database_init_and_state_load(self, env: Path) -> None:
        """DB initializes with new schema, all tables exist."""
        from rolemesh.db import get_all_tenants

        tenants = await get_all_tenants()
        assert isinstance(tenants, list)

    async def test_full_entity_creation_flow(self, env: Path) -> None:
        """Create tenant → role → coworker → binding → conversation → session."""
        from rolemesh.db import (
            create_channel_binding,
            create_conversation,
            create_coworker,
            create_tenant,
            get_session,
            set_session,
        )

        t = await create_tenant(name="Acme Corp", slug="acme")
        cw = await create_coworker(
            tenant_id=t.id,
            name="Ops Bot",
            folder="ops-bot",
            agent_role="super_agent",
        )
        b = await create_channel_binding(
            coworker_id=cw.id,
            tenant_id=t.id,
            channel_type="telegram",
            credentials={"bot_token": "xxx"},
        )
        conv = await create_conversation(
            tenant_id=t.id,
            coworker_id=cw.id,
            channel_binding_id=b.id,
            channel_chat_id="-1001234",
            name="Ops Team",
            requires_trigger=True,
        )

        # Set and get session
        await set_session(conv.id, t.id, cw.id, "sess-001")
        assert await get_session(conv.id, tenant_id=t.id) == "sess-001"

        # Verify all fields
        assert cw.agent_role == "super_agent"
        assert conv.requires_trigger is True
        assert b.channel_type == "telegram"


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
                sender="alice",
                sender_name="Alice",
                content="Hello Andy!",
                timestamp="2024-06-01T12:00:00Z",
            ),
            NewMessage(
                id="msg-2",
                chat_jid="group@test",
                sender="bob",
                sender_name="Bob",
                content="Can you summarize the meeting notes?",
                timestamp="2024-06-01T12:00:05Z",
            ),
        ]

        formatted = format_messages(messages, "UTC")
        assert "<messages>" in formatted
        assert 'sender="Alice"' in formatted
        assert "Hello Andy!" in formatted
        assert "<context timezone=" in formatted

    async def test_internal_tags_stripped(self, env: Path) -> None:
        """<internal> tags are removed from agent response."""
        from rolemesh.orchestration.router import format_outbound

        text = "Visible <internal>hidden</internal> more visible"
        cleaned = format_outbound(text)
        assert "hidden" not in cleaned
        assert "Visible" in cleaned
        assert "more visible" in cleaned


class TestScenarioIPCFromContainer:
    """Container sends IPC to schedule tasks."""

    async def test_container_schedules_cron_task(self, env: Path) -> None:
        """Agent creates a task via IPC handler."""
        from rolemesh.db import (
            create_coworker,
            create_tenant,
            get_task_by_id,
        )
        from rolemesh.ipc.task_handler import process_task_ipc

        t = await create_tenant(name="T", slug="t-ipc")
        cw = await create_coworker(tenant_id=t.id, name="Bot", folder="bot")

        from rolemesh.auth.permissions import AgentPermissions

        tasks_changed: list[bool] = []

        class FakeDeps:
            async def send_message(self, jid: str, text: str) -> None:
                pass

            async def on_tasks_changed(self) -> None:
                tasks_changed.append(True)

        import uuid

        task_id = str(uuid.uuid4())

        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "Daily standup summary",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * 1-5",
                "taskId": task_id,
                "targetCoworkerId": cw.id,
            },
            "bot",
            AgentPermissions.for_role("super_agent"),
            FakeDeps(),  # type: ignore[arg-type]
            tenant_id=t.id,
            coworker_id=cw.id,
        )

        task = await get_task_by_id(task_id, tenant_id=t.id)
        assert task is not None
        assert task.prompt == "Daily standup summary"
        assert task.schedule_type == "cron"
        assert len(tasks_changed) == 1


class TestScenarioTaskScheduling:
    """Scheduled tasks compute next_run correctly."""

    async def test_cron_schedule_computes_next_run(self, env: Path) -> None:
        from rolemesh.core.types import ScheduledTask
        from rolemesh.orchestration.task_scheduler import compute_next_run

        task = ScheduledTask(
            id="cron-1",
            tenant_id="t",
            coworker_id="cw",
            prompt="Daily check",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2024-01-01T09:00:00Z",
            status="active",
        )
        next_run = compute_next_run(task)
        assert next_run is not None
        assert "T09:00:00" in next_run

    async def test_once_schedule_returns_none(self, env: Path) -> None:
        from rolemesh.core.types import ScheduledTask
        from rolemesh.orchestration.task_scheduler import compute_next_run

        task = ScheduledTask(
            id="once-1",
            tenant_id="t",
            coworker_id="cw",
            prompt="One-time",
            schedule_type="once",
            schedule_value="2024-01-01T00:00:00",
            context_mode="isolated",
            next_run="2024-01-01T00:00:00Z",
            status="active",
        )
        assert compute_next_run(task) is None


class TestScenarioGroupQueueConcurrency:
    """Multiple groups enqueue concurrently, respecting limits."""

    async def test_multiple_groups_processed(self, env: Path) -> None:
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

    async def test_three_level_concurrency(self, env: Path) -> None:
        """OrchestratorState enforces global + tenant + coworker limits."""
        from rolemesh.core.orchestrator_state import OrchestratorState
        from rolemesh.core.types import Tenant

        state = OrchestratorState(global_limit=2)
        state.tenants["t1"] = Tenant(id="t1", name="T1", max_concurrent_containers=2)

        assert state.can_start_container("t1", "cw1") is True
        state.increment_active("t1", "cw1")
        state.increment_active("t1", "cw2")
        assert state.can_start_container("t1", "cw3") is False  # global limit = 2

        state.decrement_active("t1", "cw1")
        assert state.can_start_container("t1", "cw3") is True


class TestScenarioCredentialProxy:
    async def test_proxy_starts_and_serves(self, env: Path) -> None:
        import aiohttp

        from rolemesh.security.credential_proxy import start_credential_proxy

        runner = await start_credential_proxy(port=0, host="127.0.0.1")

        try:
            port = None
            for site in runner.sites:
                if hasattr(site, "_server") and site._server and site._server.sockets:
                    port = site._server.sockets[0].getsockname()[1]
                    break

            if port is None:
                pytest.skip("Could not determine proxy port")

            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(
                        f"http://127.0.0.1:{port}/v1/messages",
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as resp:
                        assert resp.status > 0
                except (aiohttp.ClientError, OSError):
                    pass
        finally:
            await runner.cleanup()


class TestScenarioSenderAllowlist:
    async def test_drop_mode_blocks_unauthorized(self, env: Path) -> None:
        from rolemesh.core.types import NewMessage
        from rolemesh.security.sender_allowlist import (
            ChatAllowlistEntry,
            SenderAllowlistConfig,
            is_sender_allowed,
            should_drop_message,
        )

        cfg = SenderAllowlistConfig(
            default=ChatAllowlistEntry(allow=["admin_user"], mode="drop"),
        )

        msg = NewMessage(
            id="blocked-1",
            chat_jid="group@test",
            sender="random_user",
            sender_name="Random",
            content="@Andy do something",
            timestamp="2024-06-01T12:00:01Z",
        )

        dropped = should_drop_message(msg.chat_jid, cfg) and not is_sender_allowed(msg.chat_jid, msg.sender, cfg)
        assert dropped is True
        assert is_sender_allowed("group@test", "admin_user", cfg) is True


class TestScenarioMountSecurity:
    async def test_blocked_paths_rejected(self, env: Path) -> None:
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
                is_super_agent=True,
            )
            assert not result.allowed


class TestScenarioDatabaseOperations:
    async def test_message_store_and_query_per_conversation(self, env: Path) -> None:
        """Messages stored per conversation with TIMESTAMPTZ."""
        from rolemesh.db import (
            create_channel_binding,
            create_conversation,
            create_coworker,
            create_tenant,
            get_messages_since,
            store_message,
        )

        t = await create_tenant(name="T", slug="t-db")
        cw = await create_coworker(tenant_id=t.id, name="Bot", folder="bot-db")
        b = await create_channel_binding(coworker_id=cw.id, tenant_id=t.id, channel_type="tg")
        conv = await create_conversation(
            tenant_id=t.id,
            coworker_id=cw.id,
            channel_binding_id=b.id,
            channel_chat_id="777",
        )

        for i in range(5):
            await store_message(
                tenant_id=t.id,
                conversation_id=conv.id,
                msg_id=f"msg-{i}",
                sender="user",
                sender_name="User",
                content=f"Message {i}",
                timestamp=f"2024-06-01T12:00:0{i}+00:00",
            )

        all_msgs = await get_messages_since(t.id, conv.id, "", "Bot")
        assert len(all_msgs) == 5

        since = await get_messages_since(t.id, conv.id, "2024-06-01T12:00:02+00:00", "Bot")
        assert len(since) == 2  # messages 3 and 4

    async def test_task_crud_per_coworker(self, env: Path) -> None:
        """Tasks are CRUD-able per coworker."""
        import uuid

        from rolemesh.core.types import ScheduledTask
        from rolemesh.db import (
            create_coworker,
            create_task,
            create_tenant,
            delete_task,
            get_task_by_id,
            update_task,
        )

        t = await create_tenant(name="T", slug="t-task")
        cw = await create_coworker(tenant_id=t.id, name="Bot", folder="bot-task")

        task_id = str(uuid.uuid4())
        await create_task(
            ScheduledTask(
                id=task_id,
                tenant_id=t.id,
                coworker_id=cw.id,
                prompt="Check status",
                schedule_type="interval",
                schedule_value="600000",
                context_mode="isolated",
                next_run="2024-06-01T12:00:00+00:00",
                status="active",
            )
        )

        fetched = await get_task_by_id(task_id, tenant_id=t.id)
        assert fetched is not None
        assert fetched.prompt == "Check status"

        await update_task(task_id, tenant_id=t.id, status="paused")
        fetched = await get_task_by_id(task_id, tenant_id=t.id)
        assert fetched is not None
        assert fetched.status == "paused"

        await delete_task(task_id, tenant_id=t.id)
        assert await get_task_by_id(task_id, tenant_id=t.id) is None
