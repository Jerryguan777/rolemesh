"""Live run attribution for terminal writes (single-writer groundwork).

``_process_conversation_messages`` used to keep the run it terminal-writes
in a LOCAL variable captured at turn start. Correct for the cold start —
wrong for a warm container: follow-ups piped via ``send_message`` join the
in-flight batch, but the ``_on_output`` closure still held the old run, so
the batch's terminal events were attributed to a run that already ended.

The fix moves the value to ``ConversationState.active_run_id``: the
warm-pipe path re-points it and the closure reads it live. These tests pin
exactly that seam — the closure must observe a mid-flight re-point, not
its captured snapshot.
"""

from __future__ import annotations

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
    monkeypatch.setattr("rolemesh.core.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("rolemesh.core.config.GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr("rolemesh.core.config.STORE_DIR", tmp_path / "store")

    from rolemesh.db import _init_test_database

    await _init_test_database(pg_url)

    yield tmp_path

    from rolemesh.db import close_database

    await close_database()


class RepointingExecutor:
    """Executor that re-points conv_state.active_run_id mid-flight (the
    warm-pipe effect) before emitting its final output.

    ``repoint_to=None`` leaves the field alone — the plain cold-start
    case.
    """

    def __init__(self, conv_state: ConversationState | None = None,
                 repoint_to: str | None = None) -> None:
        self._conv_state = conv_state
        self._repoint_to = repoint_to

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
        if self._conv_state is not None and self._repoint_to is not None:
            # Simulates the warm-pipe path updating the field while the
            # container is mid-batch — the closure must see this.
            self._conv_state.active_run_id = self._repoint_to
        output = AgentOutput(status="success", result="hi", is_final=True)
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


async def _seed_scenario(
    gateway: MockGateway, *, bind_run: bool
) -> tuple[ConversationState, Conversation, str | None]:
    """Tenant + coworker + telegram conversation + one unanswered message,
    optionally bound to a real ``runs`` row (messages.run_id has an FK)."""
    import rolemesh.main as m
    from rolemesh.db import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
        store_message,
        tenant_conn,
    )
    from rolemesh.runs import create_run

    t = await create_tenant(name="T", slug=f"attr-{uuid.uuid4().hex[:8]}")
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
    run_id: str | None = None
    if bind_run:
        async with tenant_conn(t.id) as conn:
            run_id = await create_run(
                tenant_id=t.id, conversation_id=conv.id, conn=conn
            )
    await store_message(
        tenant_id=t.id,
        conversation_id=conv.id,
        msg_id=f"msg-{uuid.uuid4().hex[:8]}",
        sender="user-1",
        sender_name="Alice",
        content="hello",
        timestamp="2024-06-01T12:00:01+00:00",
        run_id=run_id,
    )

    cw_state = CoworkerState.from_coworker(cw)
    cw_state.channel_bindings[binding.channel_type] = binding
    conv_state = ConversationState(conversation=conv)
    cw_state.conversations[conv.id] = conv_state
    state = OrchestratorState()
    state.coworkers[cw.id] = cw_state

    m._state = state
    m._gateways = {"telegram": gateway}  # type: ignore[assignment]
    m._queue = m.GroupQueue()
    m._queue.set_process_messages_fn(m._process_conversation_messages)
    m._transport = None
    return conv_state, conv, run_id


def _capture_terminations(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    import rolemesh.main as m

    seen: list[str | None] = []

    async def _fake_terminate(
        run_id: str | None,
        tenant_id: str,
        *,
        success: bool,
        usage: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        seen.append(run_id)

    monkeypatch.setattr(m, "_terminate_run_safe", _fake_terminate)
    return seen


def _install_executor(executor: object) -> None:
    import rolemesh.main as m

    m._executor = executor  # type: ignore[assignment]
    m._executors = {"claude": executor, "pi": executor}  # type: ignore[assignment]


async def test_turn_start_points_conv_state_at_message_run(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rolemesh.main as m

    seen = _capture_terminations(monkeypatch)
    gateway = MockGateway()
    conv_state, conv, run_id = await _seed_scenario(gateway, bind_run=True)
    _install_executor(RepointingExecutor())

    assert await m._process_conversation_messages(conv.id) is True
    assert run_id is not None
    assert conv_state.active_run_id == run_id
    assert seen == [run_id]


async def test_closure_reads_repointed_run_not_its_snapshot(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm-pipe case in miniature: the field is re-pointed while the
    executor is mid-batch; the terminal write must land on the NEW run.
    A closure that snapshotted the value at turn start fails this."""
    import rolemesh.main as m

    seen = _capture_terminations(monkeypatch)
    gateway = MockGateway()
    conv_state, conv, _old_run = await _seed_scenario(gateway, bind_run=True)
    _install_executor(RepointingExecutor(conv_state, repoint_to="run-warm"))

    assert await m._process_conversation_messages(conv.id) is True
    assert seen == ["run-warm"]


async def test_no_run_turn_passes_none(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IM turns have no run — the terminal write must receive None (its
    guard no-ops), not a stale id from a previous turn."""
    import rolemesh.main as m

    seen = _capture_terminations(monkeypatch)
    gateway = MockGateway()
    conv_state, conv, _none = await _seed_scenario(gateway, bind_run=False)
    conv_state.active_run_id = "stale-from-previous-turn"
    _install_executor(RepointingExecutor())

    assert await m._process_conversation_messages(conv.id) is True
    assert conv_state.active_run_id is None, (
        "turn start must overwrite, not inherit, the previous turn's run"
    )
    assert seen == [None]
