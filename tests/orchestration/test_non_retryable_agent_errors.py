"""Retryable vs non-retryable agent failures (orchestrator decision).

The orchestrator's only failure semantics used to be "roll back the
cursor and walk the 5→10→20→40→80s backoff ladder". That is right for
transient faults (pod crash, NATS blip, upstream 5xx) and physically
wrong for deterministic configuration errors (a tool name violating a
provider contract, an unresolvable model id): six fresh pods hit the
identical wall and the message is silently dropped ~minutes later.

The contract under test:

* ``status="error", retryable=False`` → message consumed ONCE (no
  cursor rollback, ``_process_conversation_messages`` returns True so
  the scheduler never schedules a retry), and an explanatory bot
  message reaches the conversation's channel.
* ``status="error"`` with retryable unset/True → the EXISTING path,
  byte-for-byte: cursor rolled back, False returned, no bot message.
  This is the fail-open default — anything unclassified must retry.
* Retry budget exhaustion → the scheduler fires the dropped callback
  so the retryable path's terminal outcome is also user-visible.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from rolemesh.core.orchestrator_state import (
    ConversationState,
    CoworkerState,
    OrchestratorState,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from rolemesh.core.types import Conversation

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> Path:
    data_dir = tmp_path / "data"
    groups_dir = tmp_path / "groups"
    monkeypatch.setattr("rolemesh.core.config.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.config.GROUPS_DIR", groups_dir)
    monkeypatch.setattr("rolemesh.core.config.STORE_DIR", tmp_path / "store")
    monkeypatch.setattr("rolemesh.core.config.TIMEZONE", "UTC")
    monkeypatch.setattr("rolemesh.core.group_folder.DATA_DIR", data_dir)
    monkeypatch.setattr("rolemesh.core.group_folder.GROUPS_DIR", groups_dir)

    from rolemesh.db import _init_test_database

    await _init_test_database(pg_url)

    yield tmp_path

    from rolemesh.db import close_database

    await close_database()


# ---------------------------------------------------------------------------
# Minimal harness (same shape as test_multi_tenant_e2e, kept self-contained)
# ---------------------------------------------------------------------------


class ErrorExecutor:
    """Executor whose turn ends in status="error" with a chosen retryable."""

    def __init__(self, *, retryable: bool, error: str = "boom") -> None:
        self._retryable = retryable
        self._error = error

    @property
    def name(self) -> str:
        return "mock"

    async def execute(
        self,
        inp: object,
        on_process: Callable[..., None],
        on_output: Callable[..., Awaitable[None]] | None = None,
    ) -> object:
        from rolemesh.agent import AgentOutput

        on_process("mock-container", f"job-{uuid.uuid4().hex[:6]}")
        output = AgentOutput(
            status="error", result=None, error=self._error, retryable=self._retryable
        )
        if on_output:
            await on_output(output)
        return output


@dataclass
class MockGateway:
    _channel_type: str = "telegram"
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def channel_type(self) -> str:
        return self._channel_type

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        self.sent.append((binding_id, chat_id, text))

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        pass


async def _seed_scenario(executor: object, gateway: MockGateway) -> tuple[str, Conversation]:
    """Tenant + coworker + telegram conversation + one unanswered message."""
    import rolemesh.main as m
    from rolemesh.db import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
        store_message,
    )

    t = await create_tenant(name="T", slug=f"nre-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id, name="Bot", folder=f"f-{uuid.uuid4().hex[:8]}"
    )
    binding = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "tok"},
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=binding.id,
        channel_chat_id="chat-1",
    )
    await store_message(
        tenant_id=t.id,
        conversation_id=conv.id,
        msg_id=f"msg-{uuid.uuid4().hex[:8]}",
        sender="user-1",
        sender_name="Alice",
        content="hello",
        timestamp="2024-06-01T12:00:01+00:00",
    )

    cw_state = CoworkerState.from_coworker(cw)
    cw_state.channel_bindings[binding.channel_type] = binding
    cw_state.conversations[conv.id] = ConversationState(conversation=conv)
    state = OrchestratorState()
    state.coworkers[cw.id] = cw_state

    m._state = state
    m._executor = executor  # type: ignore[assignment]
    m._executors = {"claude": executor, "pi": executor}  # type: ignore[assignment]
    m._gateways = {"telegram": gateway}  # type: ignore[assignment]
    m._queue = m.GroupQueue()
    m._queue.set_process_messages_fn(m._process_conversation_messages)
    m._transport = None
    return cw.id, conv


# ---------------------------------------------------------------------------
# Orchestrator decision branch
# ---------------------------------------------------------------------------


async def test_non_retryable_error_consumes_message_and_notifies(env: Path) -> None:
    """retryable=False → single failure: True returned (no retry), cursor NOT
    rolled back, and the user sees a configuration-error message."""
    import rolemesh.main as m

    gateway = MockGateway()
    executor = ErrorExecutor(
        retryable=False, error="Bedrock Converse: tool name 'x'*70 is invalid"
    )
    cw_id, conv = await _seed_scenario(executor, gateway)

    result = await m._process_conversation_messages(conv.id)

    assert result is True, "non-retryable error must consume the message"
    conv_state = m._state.coworkers[cw_id].conversations[conv.id]
    assert conv_state.last_agent_timestamp == "2024-06-01T12:00:01+00:00", (
        "cursor must NOT be rolled back — rollback re-queues the message"
    )
    error_notices = [text for (_b, _c, text) in gateway.sent if "Configuration error" in text]
    assert error_notices, f"user must see the config error; sent={gateway.sent}"
    assert "Bedrock Converse" in error_notices[0]


async def test_retryable_error_rolls_back_cursor_for_retry(env: Path) -> None:
    """retryable=True (explicit or default) → existing behavior untouched:
    False returned, cursor rolled back, no bot-side error message."""
    import rolemesh.main as m

    gateway = MockGateway()
    executor = ErrorExecutor(retryable=True, error="Container crashed")
    cw_id, conv = await _seed_scenario(executor, gateway)

    result = await m._process_conversation_messages(conv.id)

    assert result is False, "retryable error must go to the retry ladder"
    conv_state = m._state.coworkers[cw_id].conversations[conv.id]
    assert conv_state.last_agent_timestamp != "2024-06-01T12:00:01+00:00", (
        "cursor must be rolled back so the retry re-reads the message"
    )
    assert not gateway.sent, "retryable path must not message the user per-attempt"


async def test_default_agent_output_is_retryable(env: Path) -> None:
    """An AgentOutput built without the new field keeps retry semantics —
    the wire default protects events from older containers."""
    from rolemesh.agent import AgentOutput

    out = AgentOutput(status="error", result=None, error="x")
    assert out.retryable is True


# ---------------------------------------------------------------------------
# Wire parsing (orchestrator side of the NATS event)
# ---------------------------------------------------------------------------


async def test_parse_container_output_retryable_false() -> None:
    from rolemesh.agent.container_executor import _parse_container_output

    parsed = _parse_container_output(
        {"status": "error", "result": None, "error": "cfg", "retryable": False}
    )
    assert parsed.retryable is False


async def test_parse_container_output_retryable_absent_defaults_true() -> None:
    """Events from older containers carry no field — must stay retryable."""
    from rolemesh.agent.container_executor import _parse_container_output

    parsed = _parse_container_output({"status": "error", "result": None, "error": "x"})
    assert parsed.retryable is True


async def test_parse_container_output_retryable_junk_defaults_true() -> None:
    """A non-bool value must not be truth-coerced into suppressing retries."""
    from rolemesh.agent.container_executor import _parse_container_output

    parsed = _parse_container_output(
        {"status": "error", "result": None, "retryable": "false"}
    )
    assert parsed.retryable is True


# ---------------------------------------------------------------------------
# Scheduler: retry exhaustion fires the dropped callback
# ---------------------------------------------------------------------------


async def test_retry_exhaustion_fires_dropped_callback() -> None:
    from rolemesh.container.scheduler import _MAX_RETRIES, GroupQueue

    q = GroupQueue()
    dropped: list[str] = []

    async def _on_dropped(group_jid: str) -> None:
        dropped.append(group_jid)

    q.set_on_messages_dropped(_on_dropped)
    state = q._get_group("conv-1")

    # Below the budget: schedules a backoff, no drop.
    q._schedule_retry("conv-1", state)
    await asyncio.sleep(0)
    assert dropped == []

    # Exhaust the budget: the drop callback must fire exactly once.
    state.retry_count = _MAX_RETRIES
    q._schedule_retry("conv-1", state)
    await asyncio.sleep(0)
    assert dropped == ["conv-1"]
    assert state.retry_count == 0, "budget resets so a future message can run"

    await q.shutdown()
