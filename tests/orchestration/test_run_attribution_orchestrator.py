"""Orchestrator side of run attribution (single-writer refactor).

``_process_conversation_messages`` used to terminal-write whatever run its
closure captured at turn start (``active_run_id``) — correct for the cold
start, stale for follow-ups piped into a warm container. The container now
echoes the run of the prompt it actually served on each output event
(``AgentOutput.run_id``), and the terminal-write sites prefer that echo:
``result.run_id or active_run_id``.

The contract under test:

* The initial prompt's run is threaded into ``AgentInput.run_id`` so the
  container can seed its attribution.
* An echoed run_id wins over the closure at every terminal-write site
  (success, retryable error, non-retryable error).
* No echo (older container) → the closure fallback keeps today's
  behavior byte-for-byte.
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

    from rolemesh.agent import AgentOutput
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
# Harness (same shape as test_non_retryable_agent_errors, kept self-contained)
# ---------------------------------------------------------------------------


class EchoExecutor:
    """Executor that emits one AgentOutput built by the test, and records
    the AgentInput it was given (to assert run_id threading)."""

    def __init__(self, output: AgentOutput) -> None:
        self._output = output
        self.inputs: list[object] = []

    @property
    def name(self) -> str:
        return "mock"

    async def execute(
        self,
        inp: object,
        on_process: Callable[..., None],
        on_output: Callable[..., Awaitable[None]] | None = None,
    ) -> object:
        self.inputs.append(inp)
        on_process("mock-container", f"job-{uuid.uuid4().hex[:6]}")
        if on_output:
            await on_output(self._output)
        return self._output


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
    executor: object, gateway: MockGateway, *, bind_run: bool
) -> tuple[str, Conversation, str | None]:
    """Tenant + coworker + telegram conversation + one unanswered message.

    ``bind_run=True`` creates a real ``runs`` row (messages.run_id has an
    FK) and binds the message to it — that id becomes the closure's
    ``active_run_id``. Returns it as the third element (None otherwise)."""
    import rolemesh.main as m
    from rolemesh.db import (
        create_channel_binding,
        create_conversation,
        create_coworker,
        create_tenant,
        store_message,
        tenant_conn,
    )
    from rolemesh.runs.lifecycle import create_run

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
    return cw.id, conv, run_id


def _capture_terminations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    import rolemesh.main as m

    calls: list[dict[str, object]] = []

    async def _fake_terminate(
        run_id: str | None,
        tenant_id: str,
        *,
        success: bool,
        usage: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> None:
        calls.append(
            {"run_id": run_id, "success": success, "error": error}
        )

    monkeypatch.setattr(m, "_terminate_run_safe", _fake_terminate)
    return calls


# ---------------------------------------------------------------------------
# run_id threading into the container
# ---------------------------------------------------------------------------


async def test_active_run_id_threaded_into_agent_input(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rolemesh.main as m
    from rolemesh.agent import AgentOutput

    _capture_terminations(monkeypatch)
    executor = EchoExecutor(
        AgentOutput(status="success", result="hi", is_final=True)
    )
    _cw_id, conv, closure_run = await _seed_scenario(
        executor, MockGateway(), bind_run=True
    )

    assert await m._process_conversation_messages(conv.id) is True
    assert len(executor.inputs) == 1
    assert executor.inputs[0].run_id == closure_run, (
        "the closure's active_run_id must seed the container's attribution"
    )


# ---------------------------------------------------------------------------
# Echo wins over the closure at every terminal-write site
# ---------------------------------------------------------------------------


async def test_success_prefers_echoed_run_id(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warm-follow-up case in miniature: the container answers a NEWER
    run than the one the closure captured — the echo must win, or the new
    run's row is stranded while the old row double-terminates as a no-op."""
    import rolemesh.main as m
    from rolemesh.agent import AgentOutput

    calls = _capture_terminations(monkeypatch)
    executor = EchoExecutor(
        AgentOutput(
            status="success", result="hi", is_final=True, run_id="run-echo"
        )
    )
    _cw_id, conv, closure_run = await _seed_scenario(
        executor, MockGateway(), bind_run=True
    )

    assert await m._process_conversation_messages(conv.id) is True
    assert closure_run is not None
    assert [c["run_id"] for c in calls if c["success"]] == ["run-echo"]


async def test_retryable_error_prefers_echoed_run_id(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rolemesh.main as m
    from rolemesh.agent import AgentOutput

    calls = _capture_terminations(monkeypatch)
    executor = EchoExecutor(
        AgentOutput(
            status="error", result=None, error="boom", run_id="run-echo"
        )
    )
    _cw_id, conv, _closure_run = await _seed_scenario(
        executor, MockGateway(), bind_run=True
    )

    await m._process_conversation_messages(conv.id)
    failures = [c for c in calls if not c["success"]]
    assert [c["run_id"] for c in failures] == ["run-echo"]


async def test_non_retryable_error_prefers_echoed_run_id(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rolemesh.main as m
    from rolemesh.agent import AgentOutput

    calls = _capture_terminations(monkeypatch)
    executor = EchoExecutor(
        AgentOutput(
            status="error",
            result=None,
            error="bad config",
            retryable=False,
            run_id="run-echo",
        )
    )
    _cw_id, conv, _closure_run = await _seed_scenario(
        executor, MockGateway(), bind_run=True
    )

    assert await m._process_conversation_messages(conv.id) is True
    failures = [c for c in calls if not c["success"]]
    assert [c["run_id"] for c in failures] == ["run-echo"]
    assert failures[0]["error"] == {"code": "CONFIG_ERROR", "message": "bad config"}


# ---------------------------------------------------------------------------
# Fallback: no echo → closure behavior unchanged
# ---------------------------------------------------------------------------


async def test_no_echo_falls_back_to_closure_run_id(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events from an older container carry no run_id — the pre-refactor
    closure attribution must keep working unchanged."""
    import rolemesh.main as m
    from rolemesh.agent import AgentOutput

    calls = _capture_terminations(monkeypatch)
    executor = EchoExecutor(
        AgentOutput(status="success", result="hi", is_final=True)
    )
    _cw_id, conv, closure_run = await _seed_scenario(
        executor, MockGateway(), bind_run=True
    )

    assert await m._process_conversation_messages(conv.id) is True
    assert closure_run is not None
    assert [c["run_id"] for c in calls if c["success"]] == [closure_run]


async def test_no_echo_no_closure_terminates_nothing(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IM turns have no run at all: the terminal write must stay a no-op
    (run_id=None reaches _terminate_run_safe, whose guard returns early —
    here we just assert None is what's passed)."""
    import rolemesh.main as m
    from rolemesh.agent import AgentOutput

    calls = _capture_terminations(monkeypatch)
    executor = EchoExecutor(
        AgentOutput(status="success", result="hi", is_final=True)
    )
    _cw_id, conv, _closure_run = await _seed_scenario(
        executor, MockGateway(), bind_run=False
    )

    assert await m._process_conversation_messages(conv.id) is True
    assert [c["run_id"] for c in calls if c["success"]] == [None]
