"""End-to-end tests for multi-tenant multi-coworker architecture.

Designed from the USER's perspective: what happens when a real organization
sets up RoleMesh with multiple AI coworkers, multiple chat groups, and
concurrent usage?

Each test scenario simulates a realistic user workflow and verifies
the system behaves correctly across tenant/coworker/conversation boundaries.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerConfig,
    CoworkerState,
    OrchestratorState,
)
from rolemesh.core.types import (
    ChannelBinding,
    Conversation,
    Coworker,
    Tenant,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> Path:
    """Isolated environment with PG test DB and tmp dirs."""
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    data_dir.mkdir()
    groups_dir.mkdir()

    monkeypatch.setattr("rolemesh.core.config.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.config.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("rolemesh.core.config.STORE_DIR", tmp_path / "store")
    monkeypatch.setattr("rolemesh.core.config.POLL_INTERVAL", 0.05)
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


# ---------------------------------------------------------------------------
# Helpers: build full entity chains in DB + OrchestratorState
# ---------------------------------------------------------------------------


async def _create_tenant_in_db(name: str, slug: str, max_containers: int = 5) -> Tenant:
    from rolemesh.db import create_tenant

    return await create_tenant(name=name, slug=slug, max_concurrent_containers=max_containers)


async def _create_coworker_full(
    tenant_id: str,
    name: str,
    folder: str,
    agent_role: str = "agent",
    max_concurrent: int = 2,
    channel_type: str = "telegram",
    credentials: dict[str, str] | None = None,
    chat_ids: list[str] | None = None,
    requires_trigger: bool = True,
) -> tuple[Coworker, ChannelBinding, list[Conversation]]:
    """Create coworker + binding + conversations in DB. Returns all entities."""
    from rolemesh.db import create_channel_binding, create_conversation, create_coworker

    cw = await create_coworker(
        tenant_id=tenant_id,
        name=name,
        folder=folder,
        agent_role=agent_role,
        max_concurrent=max_concurrent,
    )
    binding = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=tenant_id,
        channel_type=channel_type,
        credentials=credentials or {"bot_token": f"token-{folder}"},
        bot_display_name=name,
    )
    conversations = []
    for chat_id in chat_ids or []:
        conv = await create_conversation(
            tenant_id=tenant_id,
            coworker_id=cw.id,
            channel_binding_id=binding.id,
            channel_chat_id=chat_id,
            name=f"Chat {chat_id}",
            requires_trigger=requires_trigger,
        )
        conversations.append(conv)
    return cw, binding, conversations


def _build_coworker_state(
    cw: Coworker,
    binding: ChannelBinding,
    conversations: list[Conversation],
) -> CoworkerState:
    """Build runtime CoworkerState from DB entities."""
    config = CoworkerConfig(
        id=cw.id,
        tenant_id=cw.tenant_id,
        name=cw.name,
        folder=cw.folder,
        system_prompt=cw.system_prompt,
        trigger_pattern=CoworkerConfig.build_trigger_pattern(cw.name),
        agent_backend=cw.agent_backend,
        container_image=None,
        max_concurrent=cw.max_concurrent,
        agent_role=cw.agent_role,
    )
    state = CoworkerState(config=config)
    state.channel_bindings[binding.channel_type] = binding
    for conv in conversations:
        state.conversations[conv.id] = ConversationState(conversation=conv)
    return state


# ---------------------------------------------------------------------------
# Mock executor: simulates agent container
# ---------------------------------------------------------------------------


@dataclass
class CapturedExecution:
    coworker_name: str
    chat_id: str
    prompt_snippet: str
    tenant_id: str
    coworker_id: str
    conversation_id: str


class MockExecutor:
    """Records all agent invocations and returns canned responses."""

    def __init__(self, response: str = "OK", session_id: str | None = "sess-new") -> None:
        self._response = response
        self._session_id = session_id
        self.executions: list[CapturedExecution] = []

    @property
    def name(self) -> str:
        return "mock"

    async def execute(
        self,
        inp: object,
        on_process: Callable[..., None],
        on_output: Callable[..., Awaitable[None]] | None = None,
    ) -> object:
        from rolemesh.agent import AgentInput, AgentOutput

        assert isinstance(inp, AgentInput)
        self.executions.append(
            CapturedExecution(
                coworker_name=inp.assistant_name or "",
                chat_id=inp.chat_jid,
                prompt_snippet=inp.prompt[:100],
                tenant_id=inp.tenant_id,
                coworker_id=inp.coworker_id,
                conversation_id=inp.conversation_id,
            )
        )
        on_process("mock-container", f"job-{uuid.uuid4().hex[:6]}")
        output = AgentOutput(
            status="success",
            result=self._response,
            new_session_id=self._session_id,
        )
        if on_output:
            await on_output(output)
        return AgentOutput(status="success", result=None, new_session_id=self._session_id)


class FailingExecutor(MockExecutor):
    """Executor that simulates agent failure."""

    def __init__(self) -> None:
        super().__init__()

    async def execute(
        self,
        inp: object,
        on_process: Callable[..., None],
        on_output: Callable[..., Awaitable[None]] | None = None,
    ) -> object:
        from rolemesh.agent import AgentInput, AgentOutput

        assert isinstance(inp, AgentInput)
        self.executions.append(
            CapturedExecution(
                coworker_name=inp.assistant_name or "",
                chat_id=inp.chat_jid,
                prompt_snippet=inp.prompt[:100],
                tenant_id=inp.tenant_id,
                coworker_id=inp.coworker_id,
                conversation_id=inp.conversation_id,
            )
        )
        on_process("crash-container", f"job-{uuid.uuid4().hex[:6]}")
        if on_output:
            await on_output(AgentOutput(status="error", result=None, error="Container crashed"))
        return AgentOutput(status="error", result=None, error="Container crashed")


# ---------------------------------------------------------------------------
# Mock gateway: captures sent messages + typing indicators
# ---------------------------------------------------------------------------


@dataclass
class SentMessage:
    binding_id: str
    chat_id: str
    text: str


@dataclass
class MockGateway:
    """Records all messages sent through each binding."""

    _channel_type: str = "telegram"
    sent: list[SentMessage] = field(default_factory=list)
    typing_events: list[tuple[str, str, bool]] = field(default_factory=list)

    @property
    def channel_type(self) -> str:
        return self._channel_type

    async def add_binding(self, binding: ChannelBinding) -> None:
        pass

    async def remove_binding(self, binding_id: str) -> None:
        pass

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        self.sent.append(SentMessage(binding_id=binding_id, chat_id=chat_id, text=text))

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        self.typing_events.append((binding_id, chat_id, is_typing))

    async def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helper: set up main.py module state for a test scenario
# ---------------------------------------------------------------------------


def _wire_main_state(
    state: OrchestratorState,
    executor: MockExecutor,
    gateways: dict[str, MockGateway],
) -> None:
    """Wire up main.py module-level state for testing."""
    import rolemesh.main as m

    m._state = state
    m._executor = executor  # type: ignore[assignment]
    m._executors = {"claude": executor, "claude-code": executor, "pi": executor}  # type: ignore[assignment]
    m._gateways = gateways  # type: ignore[assignment]
    m._queue = m.GroupQueue()
    m._queue.set_process_messages_fn(m._process_conversation_messages)
    m._transport = None  # Skip NATS for these tests


async def _inject_message(
    tenant_id: str,
    conversation_id: str,
    content: str,
    sender: str = "user-1",
    sender_name: str = "Alice",
    timestamp: str = "2024-06-01T12:00:01+00:00",
    msg_id: str | None = None,
) -> None:
    """Store a message directly in DB as if a channel delivered it."""
    from rolemesh.db import store_message

    await store_message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        msg_id=msg_id or f"msg-{uuid.uuid4().hex[:8]}",
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
    )


# ===========================================================================
# Scenario 1: Acme Corp — two coworkers, same group, different bots
#
# User story: "We have an Ops Bot and a CS Bot. Both are in the same
# Telegram group. @Ops Bot handles logistics, @CS Bot handles customer
# questions. Each should only respond to its own @mention."
# ===========================================================================


class TestTwoCoworkersSameGroup:
    async def test_ops_bot_only_responds_to_ops_mention(self, env: Path) -> None:
        """@Ops Bot triggers ops coworker; @CS Bot in same group does not."""
        tenant = await _create_tenant_in_db("Acme", "acme-2cw")

        ops_cw, ops_bind, ops_convs = await _create_coworker_full(
            tenant.id,
            "Ops Bot",
            "ops-bot",
            chat_ids=["-1001000"],
        )
        cs_cw, cs_bind, cs_convs = await _create_coworker_full(
            tenant.id,
            "CS Bot",
            "cs-bot",
            chat_ids=["-1001000"],  # Same group!
        )

        state = OrchestratorState(global_limit=10)
        state.tenants[tenant.id] = tenant
        state.coworkers[ops_cw.id] = _build_coworker_state(ops_cw, ops_bind, ops_convs)
        state.coworkers[cs_cw.id] = _build_coworker_state(cs_cw, cs_bind, cs_convs)

        executor = MockExecutor(response="Shipment tracking updated.")
        gateway = MockGateway()
        _wire_main_state(state, executor, {"telegram": gateway})

        # User says "@Ops Bot where is my shipment?"
        await _inject_message(tenant.id, ops_convs[0].id, "@Ops Bot where is my shipment?")

        import rolemesh.main as m

        result = await m._process_conversation_messages(ops_convs[0].id)
        assert result is True

        # Ops Bot should have been invoked
        assert len(executor.executions) == 1
        assert executor.executions[0].coworker_name == "Ops Bot"

        # Response sent to the group via ops binding
        assert len(gateway.sent) == 1
        assert gateway.sent[0].binding_id == ops_bind.id
        assert "Shipment" in gateway.sent[0].text

    async def test_message_without_any_trigger_ignored_by_both(self, env: Path) -> None:
        """A message without @mention triggers neither bot."""
        tenant = await _create_tenant_in_db("Acme", "acme-notrig")

        ops_cw, ops_bind, ops_convs = await _create_coworker_full(
            tenant.id,
            "Ops Bot",
            "ops-notrig",
            chat_ids=["-1002000"],
        )

        state = OrchestratorState(global_limit=10)
        state.tenants[tenant.id] = tenant
        state.coworkers[ops_cw.id] = _build_coworker_state(ops_cw, ops_bind, ops_convs)

        executor = MockExecutor()
        _wire_main_state(state, executor, {"telegram": MockGateway()})

        await _inject_message(tenant.id, ops_convs[0].id, "Just chatting, no bot mention")

        import rolemesh.main as m

        result = await m._process_conversation_messages(ops_convs[0].id)
        assert result is True
        assert len(executor.executions) == 0  # Neither bot invoked

    async def test_admin_coworker_skips_trigger_check(self, env: Path) -> None:
        """Admin coworker (is_main) responds to any message, no trigger needed."""
        tenant = await _create_tenant_in_db("Acme", "acme-admin")

        admin_cw, admin_bind, admin_convs = await _create_coworker_full(
            tenant.id,
            "Admin Bot",
            "admin-bot",
            agent_role="super_agent",
            chat_ids=["-1003000"],
            requires_trigger=False,
        )

        state = OrchestratorState(global_limit=10)
        state.tenants[tenant.id] = tenant
        state.coworkers[admin_cw.id] = _build_coworker_state(admin_cw, admin_bind, admin_convs)

        executor = MockExecutor(response="I'm the admin bot!")
        gateway = MockGateway()
        _wire_main_state(state, executor, {"telegram": gateway})

        # No @mention at all
        await _inject_message(tenant.id, admin_convs[0].id, "Do something for me")

        import rolemesh.main as m

        result = await m._process_conversation_messages(admin_convs[0].id)
        assert result is True
        assert len(executor.executions) == 1
        assert gateway.sent[0].text == "I'm the admin bot!"


# ===========================================================================
# Scenario 2: Session isolation across conversations
#
# User story: "Our Ops Bot is in Group A and Group B. A conversation about
# shipping in Group A shouldn't leak into the context of Group B."
# ===========================================================================


class TestSessionIsolation:
    async def test_different_conversations_get_different_sessions(self, env: Path) -> None:
        """Same coworker, two groups → independent session IDs."""
        tenant = await _create_tenant_in_db("Acme", "acme-sess")

        cw, binding, convs = await _create_coworker_full(
            tenant.id,
            "Ops Bot",
            "ops-sess",
            agent_role="super_agent",
            chat_ids=["-100A", "-100B"],
            requires_trigger=False,
        )

        state = OrchestratorState(global_limit=10)
        state.tenants[tenant.id] = tenant
        state.coworkers[cw.id] = _build_coworker_state(cw, binding, convs)

        # Executor returns different session IDs for each call
        call_count = 0

        class SessionTrackingExecutor(MockExecutor):
            async def execute(
                self,
                inp: object,
                on_process: Callable[..., None],
                on_output: Callable[..., Awaitable[None]] | None = None,
            ) -> object:
                nonlocal call_count
                call_count += 1
                self._session_id = f"sess-{call_count}"
                return await super().execute(inp, on_process, on_output)

        executor = SessionTrackingExecutor(response="Done")
        _wire_main_state(state, executor, {"telegram": MockGateway()})

        # Message in Group A
        await _inject_message(tenant.id, convs[0].id, "Task A", timestamp="2024-06-01T12:00:01+00:00")

        import rolemesh.main as m

        await m._process_conversation_messages(convs[0].id)

        # Message in Group B
        await _inject_message(tenant.id, convs[1].id, "Task B", timestamp="2024-06-01T12:00:02+00:00")
        await m._process_conversation_messages(convs[1].id)

        # Sessions should be different
        from rolemesh.db import get_session

        sess_a = await get_session(convs[0].id, tenant_id=tenant.id)
        sess_b = await get_session(convs[1].id, tenant_id=tenant.id)
        assert sess_a is not None
        assert sess_b is not None
        assert sess_a != sess_b

    async def test_cursor_rollback_is_per_conversation(self, env: Path) -> None:
        """Agent fails in Group A → only Group A's cursor rolls back, not Group B's."""
        tenant = await _create_tenant_in_db("Acme", "acme-rollback")

        cw, binding, convs = await _create_coworker_full(
            tenant.id,
            "Ops Bot",
            "ops-rb",
            agent_role="super_agent",
            chat_ids=["-200A", "-200B"],
            requires_trigger=False,
        )

        state = OrchestratorState(global_limit=10)
        state.tenants[tenant.id] = tenant
        state.coworkers[cw.id] = _build_coworker_state(cw, binding, convs)

        # First: succeed in Group B
        executor = MockExecutor(response="Success")
        _wire_main_state(state, executor, {"telegram": MockGateway()})

        await _inject_message(tenant.id, convs[1].id, "Good message", timestamp="2024-06-01T12:00:01+00:00")

        import rolemesh.main as m

        await m._process_conversation_messages(convs[1].id)
        # Group B cursor should be advanced
        b_cursor = state.coworkers[cw.id].conversations[convs[1].id].last_agent_timestamp
        assert b_cursor != ""

        # Now: fail in Group A
        fail_executor = FailingExecutor()
        m._executor = fail_executor  # type: ignore[assignment]
        m._executors = {"claude": fail_executor, "claude-code": fail_executor, "pi": fail_executor}  # type: ignore[assignment]

        await _inject_message(tenant.id, convs[0].id, "This will fail", timestamp="2024-06-01T12:00:02+00:00")
        result = await m._process_conversation_messages(convs[0].id)
        assert result is False  # Failure → retry

        # Group A cursor should be rolled back
        a_cursor = state.coworkers[cw.id].conversations[convs[0].id].last_agent_timestamp
        assert a_cursor == ""  # Rolled back to initial

        # Group B cursor should NOT be affected
        b_cursor_after = state.coworkers[cw.id].conversations[convs[1].id].last_agent_timestamp
        assert b_cursor_after == b_cursor  # Unchanged


# ===========================================================================
# Scenario 3: Three-level concurrency control
#
# User story: "Our tenant has a limit of 2 concurrent containers. Each
# coworker has a limit of 1. When both coworkers are busy, new requests
# should queue."
# ===========================================================================


class TestThreeLevelConcurrency:
    async def test_tenant_limit_blocks_third_container(self, env: Path) -> None:
        """Tenant max=2 → third coworker's container is queued."""
        state = OrchestratorState(global_limit=10)
        state.tenants["t1"] = Tenant(id="t1", name="T", max_concurrent_containers=2)

        # Start 2 containers for 2 different coworkers
        assert state.can_start_container("t1", "cw1") is True
        state.increment_active("t1", "cw1")
        assert state.can_start_container("t1", "cw2") is True
        state.increment_active("t1", "cw2")

        # Third should be blocked by tenant limit
        assert state.can_start_container("t1", "cw3") is False

        # After one finishes, third can start
        state.decrement_active("t1", "cw1")
        assert state.can_start_container("t1", "cw3") is True

    async def test_coworker_limit_blocks_second_container_for_same_coworker(self, env: Path) -> None:
        """Coworker max_concurrent=1 → second request for same coworker queued."""
        state = OrchestratorState(global_limit=10)
        state.tenants["t1"] = Tenant(id="t1", name="T", max_concurrent_containers=10)

        cw_config = CoworkerConfig(
            id="cw1",
            tenant_id="t1",
            name="Bot",
            folder="bot",
            system_prompt=None,
            trigger_pattern=CoworkerConfig.build_trigger_pattern("Bot"),
            agent_backend="claude-code",
            container_image=None,
            max_concurrent=1,
        )
        state.coworkers["cw1"] = CoworkerState(config=cw_config)

        assert state.can_start_container("t1", "cw1") is True
        state.increment_active("t1", "cw1")
        # Same coworker, second request → blocked by coworker limit
        assert state.can_start_container("t1", "cw1") is False

    async def test_global_limit_blocks_all_tenants(self, env: Path) -> None:
        """Global limit=2 → no more containers regardless of tenant."""
        state = OrchestratorState(global_limit=2)
        state.tenants["t1"] = Tenant(id="t1", name="T1", max_concurrent_containers=5)
        state.tenants["t2"] = Tenant(id="t2", name="T2", max_concurrent_containers=5)

        state.increment_active("t1", "cw1")
        state.increment_active("t1", "cw2")
        # Global limit reached
        assert state.can_start_container("t2", "cw3") is False

    async def test_queue_respects_orchestrator_state(self, env: Path) -> None:
        """GroupQueue uses OrchestratorState limits, not just a flat counter."""
        from rolemesh.container.scheduler import GroupQueue

        state = OrchestratorState(global_limit=1)
        queue = GroupQueue(orchestrator_state=state)

        started: list[str] = []

        async def process_fn(group_jid: str) -> bool:
            started.append(group_jid)
            await asyncio.sleep(0.2)
            return True

        queue.set_process_messages_fn(process_fn)
        queue.enqueue_message_check("g1", tenant_id="t1", coworker_id="cw1")
        await asyncio.sleep(0.05)

        # While g1 is running, g2 should be queued (global limit=1)
        queue.enqueue_message_check("g2", tenant_id="t1", coworker_id="cw2")
        await asyncio.sleep(0.05)
        assert queue._get_group("g2").pending_messages is True

        # After g1 finishes, g2 should drain
        await asyncio.sleep(0.4)
        assert "g1" in started
        assert "g2" in started


# ===========================================================================
# Scenario 4: IPC register_conversation
#
# User story: "The admin bot discovers a new Telegram group and registers
# it as a conversation for itself. Only admin bots can do this."
# ===========================================================================


class TestRegisterConversation:
    """register_conversation IPC was removed — these tests verify
    that the old task type is now treated as unknown and silently ignored."""

    async def test_register_conversation_now_unknown(self, env: Path) -> None:
        """register_conversation IPC type is no longer handled (moved to admin API)."""
        from rolemesh.auth.permissions import AgentPermissions
        from rolemesh.ipc.task_handler import process_task_ipc

        class FakeDeps:
            async def send_message(self, jid: str, text: str) -> None:
                pass

            async def on_tasks_changed(self) -> None:
                pass

        # Should not raise, just log unknown type
        await process_task_ipc(
            {"type": "register_conversation", "channel_chat_id": "-999"},
            "some-folder",
            AgentPermissions.for_role("super_agent"),
            FakeDeps(),  # type: ignore[arg-type]
        )


# ===========================================================================
# Scenario 5: Migration from legacy single-tenant data
#
# User story: "I've been running RoleMesh with the old single-tenant setup.
# I upgrade to Step 5 and run the migration. All my groups, sessions, and
# files should be preserved."
# ===========================================================================


class TestMigration:
    async def test_registered_groups_migrate_to_coworkers(self, env: Path) -> None:
        """Legacy registered_groups → coworker + binding + conversation."""
        from rolemesh.core.types import RegisteredGroup
        from rolemesh.db import (
            _get_pool,
            get_all_conversations,
            get_all_coworkers,
            get_all_registered_groups,
            get_session,
            set_registered_group,
            set_session_legacy,
        )

        # Create legacy tables that no longer exist in _create_schema
        pool = _get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS registered_groups (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    jid TEXT NOT NULL,
                    name TEXT NOT NULL,
                    folder TEXT NOT NULL,
                    trigger_pattern TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    container_config JSONB,
                    requires_trigger BOOLEAN DEFAULT TRUE,
                    is_main BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (tenant_id, jid),
                    UNIQUE (tenant_id, folder)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions_legacy (
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    group_folder TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, group_folder)
                )
            """)

        # Set up legacy data
        await set_registered_group(
            "tg:-1001",
            RegisteredGroup(
                name="Main Group",
                folder="main-group",
                trigger="@Andy",
                added_at="2024-01-01T00:00:00Z",
                is_main=True,
                requires_trigger=False,
            ),
        )
        await set_registered_group(
            "tg:-1002",
            RegisteredGroup(
                name="Team Chat",
                folder="team-chat",
                trigger="@Andy",
                added_at="2024-01-01T00:00:00Z",
                is_main=False,
            ),
        )
        await set_session_legacy("main-group", "sess-legacy-001")

        # Create old group dirs
        groups_dir = env / "groups"
        (groups_dir / "main-group" / "logs").mkdir(parents=True)
        (groups_dir / "main-group" / "CLAUDE.md").write_text("# Memory\n")
        (groups_dir / "team-chat").mkdir(parents=True)

        # Verify legacy data
        legacy = await get_all_registered_groups()
        assert len(legacy) == 2

        # Run migration inline (same logic as scripts/migrate_to_multi_tenant.py)
        from rolemesh.db import (
            create_channel_binding,
            create_conversation,
            create_coworker,
            create_tenant,
            get_tenant_by_slug,
            set_session,
        )

        data_dir = env / "data"

        tenant = await get_tenant_by_slug("default")
        if tenant is None:
            tenant = await create_tenant(slug="default", name="Default Tenant")

        for jid, group in legacy.items():
            prefix = "tg:"
            chat_id = jid[len(prefix) :] if jid.startswith(prefix) else jid

            coworker = await create_coworker(
                tenant_id=tenant.id,
                name=group.name,
                folder=group.folder,
                agent_role="super_agent" if group.is_main else "agent",
            )
            binding = await create_channel_binding(
                coworker_id=coworker.id,
                tenant_id=tenant.id,
                channel_type="telegram",
                credentials={"bot_token": "test"},
            )
            conv = await create_conversation(
                tenant_id=tenant.id,
                coworker_id=coworker.id,
                channel_binding_id=binding.id,
                channel_chat_id=chat_id,
                name=group.name,
                requires_trigger=group.requires_trigger,
            )

            from rolemesh.db import get_session_legacy

            old_sess = await get_session_legacy(group.folder)
            if old_sess:
                await set_session(conv.id, tenant.id, coworker.id, old_sess)

            # Filesystem migration
            import shutil

            old_dir = groups_dir / group.folder
            new_ws = data_dir / "tenants" / tenant.id / "coworkers" / group.folder / "workspace"
            if old_dir.exists() and not new_ws.exists():
                new_ws.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(str(old_dir), str(new_ws), dirs_exist_ok=True)

        # Verify new data
        coworkers = await get_all_coworkers()
        assert len(coworkers) == 2

        main_cw = next((c for c in coworkers if c.folder == "main-group"), None)
        assert main_cw is not None
        assert main_cw.agent_role == "super_agent"

        team_cw = next((c for c in coworkers if c.folder == "team-chat"), None)
        assert team_cw is not None
        assert team_cw.agent_role == "agent"

        conversations = await get_all_conversations()
        assert len(conversations) == 2

        main_conv = next((c for c in conversations if c.channel_chat_id == "-1001"), None)
        assert main_conv is not None
        assert main_conv.requires_trigger is False

        # Session migrated
        sess = await get_session(main_conv.id, tenant_id=tenant.id)
        assert sess == "sess-legacy-001"

        # Filesystem migrated (tenant already in scope from above)
        assert tenant is not None
        workspace = env / "data" / "tenants" / tenant.id / "coworkers" / "main-group" / "workspace"
        assert workspace.exists()
        assert (workspace / "CLAUDE.md").exists()


# ===========================================================================
# Scenario 6: Volume mount paths
#
# User story: "Container gets the right file mounts: workspace is shared
# across conversations, sessions are per-conversation."
# ===========================================================================


class TestVolumeMountPaths:
    def test_workspace_shared_sessions_separate(self, tmp_path: Path) -> None:
        """Same coworker, two conversations → same workspace, different session dirs."""
        from rolemesh.container.runner import build_volume_mounts

        cw = Coworker(
            id="cw1",
            tenant_id="t1",
            name="Bot",
            folder="bot-vol",
        )

        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts_a = build_volume_mounts(cw, "t1", "conv-aaa", is_main=False)
            mounts_b = build_volume_mounts(cw, "t1", "conv-bbb", is_main=False)

        def _find(mounts: list[object], container_path: str) -> str:
            for m in mounts:
                if getattr(m, "container_path", "") == container_path:
                    return getattr(m, "host_path", "")
            return ""

        # Workspace mount is identical (same coworker)
        ws_a = _find(mounts_a, "/workspace/group")
        ws_b = _find(mounts_b, "/workspace/group")
        assert ws_a == ws_b
        assert "bot-vol/workspace" in ws_a

        # Session mounts are different (per-conversation)
        sess_a = _find(mounts_a, "/workspace/sessions")
        sess_b = _find(mounts_b, "/workspace/sessions")
        assert sess_a != sess_b
        assert "conv-aaa" in sess_a
        assert "conv-bbb" in sess_b

    def test_tenant_prefix_in_paths(self, tmp_path: Path) -> None:
        """Volume paths include tenant_id."""
        from rolemesh.container.runner import build_volume_mounts

        cw = Coworker(
            id="cw1",
            tenant_id="t-acme",
            name="Bot",
            folder="bot-tp",
        )

        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(cw, "t-acme", "conv-1", is_main=False)

        host_paths = [m.host_path for m in mounts]
        # All paths should contain tenants/t-acme/coworkers/bot-tp
        workspace_paths = [p for p in host_paths if "bot-tp" in p]
        assert all("t-acme" in p for p in workspace_paths)

    def test_shared_dir_mounted_readonly(self, tmp_path: Path) -> None:
        """Tenant shared knowledge is mounted read-only."""
        from rolemesh.container.runner import build_volume_mounts

        # Create shared dir
        shared = tmp_path / "tenants" / "t1" / "shared"
        shared.mkdir(parents=True)

        cw = Coworker(id="cw1", tenant_id="t1", name="Bot", folder="bot-sh")

        with (
            patch("rolemesh.container.runner.DATA_DIR", tmp_path),
            patch("rolemesh.container.runner.PROJECT_ROOT", tmp_path),
        ):
            mounts = build_volume_mounts(cw, "t1", "conv-1", is_main=False)

        shared_mount = next((m for m in mounts if m.container_path == "/workspace/shared"), None)
        assert shared_mount is not None
        assert shared_mount.readonly is True


# ===========================================================================
# Scenario 7: Message isolation between coworkers
#
# User story: "Messages stored for Ops Bot's conversation should NOT appear
# when CS Bot queries its conversation, even if they're in the same chat."
# ===========================================================================


class TestMessageIsolation:
    async def test_messages_scoped_to_conversation(self, env: Path) -> None:
        """Two coworkers in same chat → each only sees its own conversation's messages."""
        from rolemesh.db import get_messages_since

        tenant = await _create_tenant_in_db("Acme", "acme-iso")

        _ops_cw, _ops_bind, ops_convs = await _create_coworker_full(
            tenant.id,
            "Ops Bot",
            "ops-iso",
            chat_ids=["-300"],
        )
        _cs_cw, _cs_bind, cs_convs = await _create_coworker_full(
            tenant.id,
            "CS Bot",
            "cs-iso",
            chat_ids=["-300"],
        )

        # Store message in Ops conversation
        await _inject_message(tenant.id, ops_convs[0].id, "Ops-specific logistics data")

        # Store message in CS conversation
        await _inject_message(
            tenant.id,
            cs_convs[0].id,
            "Customer complaint #1234",
            timestamp="2024-06-01T12:00:02+00:00",
        )

        # Ops Bot queries → only sees its message
        ops_msgs = await get_messages_since(tenant.id, ops_convs[0].id, "", "Ops Bot")
        assert len(ops_msgs) == 1
        assert "logistics" in ops_msgs[0].content

        # CS Bot queries → only sees its message
        cs_msgs = await get_messages_since(tenant.id, cs_convs[0].id, "", "CS Bot")
        assert len(cs_msgs) == 1
        assert "complaint" in cs_msgs[0].content


# ===========================================================================
# Scenario 8: Task scheduling per coworker
#
# User story: "Admin schedules a daily task for Ops Bot. The task should
# belong to Ops Bot's coworker_id, not a group folder. Non-admin can only
# manage their own tasks."
# ===========================================================================


class TestTaskSchedulingPerCoworker:
    async def test_task_created_with_coworker_id(self, env: Path) -> None:
        """IPC schedule_task creates task keyed by coworker_id, not folder."""
        from rolemesh.auth.permissions import AgentPermissions
        from rolemesh.db import get_task_by_id
        from rolemesh.ipc.task_handler import process_task_ipc

        tenant = await _create_tenant_in_db("Acme", "acme-task")
        cw, _, _ = await _create_coworker_full(
            tenant.id,
            "Ops Bot",
            "ops-task",
            agent_role="super_agent",
            chat_ids=["-400"],
        )

        class FakeDeps:
            async def send_message(self, jid: str, text: str) -> None:
                pass

            async def on_tasks_changed(self) -> None:
                pass

        task_id = str(uuid.uuid4())
        await process_task_ipc(
            {
                "type": "schedule_task",
                "taskId": task_id,
                "prompt": "Daily standup",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "targetCoworkerId": cw.id,
            },
            "ops-task",
            AgentPermissions.for_role("super_agent"),
            FakeDeps(),  # type: ignore[arg-type]
            tenant_id=tenant.id,
            coworker_id=cw.id,
        )

        task = await get_task_by_id(task_id, tenant_id=tenant.id)
        assert task is not None
        assert task.coworker_id == cw.id
        assert task.tenant_id == tenant.id

    async def test_non_admin_cannot_schedule_for_other_coworker(self, env: Path) -> None:
        """Non-admin trying to schedule a task for another coworker → blocked."""
        from rolemesh.auth.permissions import AgentPermissions
        from rolemesh.db import get_all_tasks
        from rolemesh.ipc.task_handler import process_task_ipc

        tenant = await _create_tenant_in_db("Acme", "acme-task-auth")

        class FakeDeps:
            async def send_message(self, jid: str, text: str) -> None:
                pass

            async def on_tasks_changed(self) -> None:
                pass

        await process_task_ipc(
            {
                "type": "schedule_task",
                "prompt": "Should be blocked",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "targetCoworkerId": "other-coworker-id",
            },
            "my-folder",
            AgentPermissions.for_role("agent"),
            FakeDeps(),  # type: ignore[arg-type]
            tenant_id=tenant.id,
            coworker_id="my-coworker-id",
        )

        # Should not have created any task
        tasks = await get_all_tasks(tenant.id)
        assert len(tasks) == 0


# ===========================================================================
# Scenario 9: Trigger pattern derived from coworker name
#
# User story: "I named my coworker 'Sales AI'. Users should @mention
# '@Sales AI' to trigger it. Case-insensitive."
# ===========================================================================


class TestTriggerPatternFromName:
    def test_trigger_pattern_matches_coworker_name(self) -> None:
        """Trigger pattern derived from coworker name, case-insensitive."""
        pattern = CoworkerConfig.build_trigger_pattern("Sales AI")
        assert pattern.search("@Sales AI what's our revenue?")
        assert pattern.search("@sales ai please help")
        assert not pattern.search("Hey sales team")  # No @mention
        assert not pattern.search("@SalesBot help")  # Wrong name

    def test_trigger_pattern_with_dot_in_name(self) -> None:
        """Dots in names are literal, not regex wildcards."""
        pattern = CoworkerConfig.build_trigger_pattern("Bot.v2")
        assert pattern.search("@Bot.v2 help me")
        assert not pattern.search("@BotXv2 help me")  # Dot must be literal

    def test_trigger_pattern_ending_in_non_word_char_known_limitation(self) -> None:
        """Known: \\b after non-word chars (like ')') won't match space boundary.

        This is a regex limitation. In practice, coworker names rarely end with
        special chars. Document the behavior rather than work around it.
        """
        pattern = CoworkerConfig.build_trigger_pattern("Bot (v2.0)")
        # \b after ) is a non-word/non-word boundary, so it doesn't fire before space
        assert pattern.search("@Bot (v2.0)") is None  # Known limitation

    def test_trigger_pattern_boundary(self) -> None:
        """@mention must be word-bounded (not just a prefix)."""
        pattern = CoworkerConfig.build_trigger_pattern("Andy")
        assert pattern.search("@Andy help")
        assert not pattern.search("@Andybot help")  # "Andy" is prefix but not word


# ===========================================================================
# Scenario 10: OrchestratorState lookups
#
# User story: "When a message comes in, the system needs to find the right
# coworker by binding_id + chat_id. This lookup must be fast and correct."
# ===========================================================================


class TestOrchestratorStateLookups:
    def test_find_conversation_by_binding_and_chat(self) -> None:
        """Lookup by binding_id + chat_id returns correct coworker + conversation."""
        state = OrchestratorState()
        binding = ChannelBinding(id="b1", coworker_id="cw1", tenant_id="t1", channel_type="telegram")
        conv = Conversation(
            id="conv1",
            tenant_id="t1",
            coworker_id="cw1",
            channel_binding_id="b1",
            channel_chat_id="-1001",
        )
        config = CoworkerConfig(
            id="cw1",
            tenant_id="t1",
            name="Bot",
            folder="bot",
            system_prompt=None,
            trigger_pattern=CoworkerConfig.build_trigger_pattern("Bot"),
            agent_backend="claude-code",
            container_image=None,
            max_concurrent=2,
        )
        cw_state = CoworkerState(config=config)
        cw_state.channel_bindings["telegram"] = binding
        cw_state.conversations["conv1"] = ConversationState(conversation=conv)
        state.coworkers["cw1"] = cw_state

        result = state.find_conversation_by_binding_and_chat("b1", "-1001")
        assert result is not None
        found_cw, found_conv = result
        assert found_cw.config.id == "cw1"
        assert found_conv.conversation.id == "conv1"

    def test_find_returns_none_for_unknown_binding(self) -> None:
        """Unknown binding_id returns None."""
        state = OrchestratorState()
        assert state.find_conversation_by_binding_and_chat("unknown", "-1001") is None

    def test_find_returns_none_for_wrong_chat_id(self) -> None:
        """Known binding but wrong chat_id returns None."""
        state = OrchestratorState()
        binding = ChannelBinding(id="b1", coworker_id="cw1", tenant_id="t1", channel_type="telegram")
        config = CoworkerConfig(
            id="cw1",
            tenant_id="t1",
            name="Bot",
            folder="bot",
            system_prompt=None,
            trigger_pattern=CoworkerConfig.build_trigger_pattern("Bot"),
            agent_backend="claude-code",
            container_image=None,
            max_concurrent=2,
        )
        cw_state = CoworkerState(config=config)
        cw_state.channel_bindings["telegram"] = binding
        # No conversations registered
        state.coworkers["cw1"] = cw_state

        assert state.find_conversation_by_binding_and_chat("b1", "-9999") is None

    def test_get_coworker_by_folder(self) -> None:
        """Lookup by tenant + folder returns correct coworker."""
        state = OrchestratorState()
        config = CoworkerConfig(
            id="cw1",
            tenant_id="t1",
            name="Bot",
            folder="my-bot",
            system_prompt=None,
            trigger_pattern=CoworkerConfig.build_trigger_pattern("Bot"),
            agent_backend="claude-code",
            container_image=None,
            max_concurrent=2,
        )
        state.coworkers["cw1"] = CoworkerState(config=config)

        found = state.get_coworker_by_folder("t1", "my-bot")
        assert found is not None
        assert found.config.id == "cw1"

        # Wrong tenant
        assert state.get_coworker_by_folder("t2", "my-bot") is None
        # Wrong folder
        assert state.get_coworker_by_folder("t1", "other-bot") is None
