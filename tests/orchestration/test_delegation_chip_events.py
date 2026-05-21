"""End-to-end chip-event tests for the delegation handler (v1.5).

Verifies that the chip emitter is driven correctly on every code path
through ``handle_delegate_request``:

  - happy path:    open → ... → close(success)
  - tool_use:      open → tool_use → close(success)
  - safety_blocked: open → close(safety_blocked)
  - business timeout: open → close(timeout)
  - closure exception: open → close(error)
  - no emitter:    handler runs cleanly with emit_chip_event=None
  - emitter raise: a buggy emit_chip_event MUST NOT corrupt the audit
                   path (handler still completes, audit row still written)

All tests use the same seeding harness as test_delegate_handler.py.
The chip emitter is a small recorder that captures (phase, payload)
pairs for assertion.

Chip events are scheduled via ``asyncio.create_task`` for the
fire-and-forget guarantee; the tests use a small ``_drain`` helper to
yield enough event-loop ticks for the open task to land before the
close task arrives, so the recorder sees calls in the same order the
production handler would publish them.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.agent.executor import AgentInput, AgentOutput
from rolemesh.container.scheduler import GroupQueue
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.db import (
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
)
from rolemesh.orchestration.delegation import handle_delegate_request

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.core.types import Coworker

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# Recorder + fakes (parallel structure to test_delegate_handler.py)
# ---------------------------------------------------------------------------


@dataclass
class FakeMsg:
    data: bytes
    replies: list[bytes] = field(default_factory=list)

    async def respond(self, body: bytes) -> None:
        self.replies.append(body)


@dataclass
class FakeExecutor:
    events: list[AgentOutput] = field(default_factory=list)
    pre_emit_delay: float = 0.0
    initial_sleep: float = 0.0
    raise_exc: BaseException | None = None
    call_on_process: bool = True

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        if self.call_on_process:
            on_process("fake-container", f"job-{uuid.uuid4().hex[:6]}")
        if self.initial_sleep:
            await asyncio.sleep(self.initial_sleep)
        if self.raise_exc is not None:
            raise self.raise_exc
        if on_output is None:
            return self.events[-1] if self.events else AgentOutput(
                status="success", result="", is_final=True,
            )
        for ev in self.events:
            if self.pre_emit_delay:
                await asyncio.sleep(self.pre_emit_delay)
            await on_output(ev)
        return self.events[-1] if self.events else AgentOutput(
            status="success", result="", is_final=True,
        )


def _executor_factory(executor: FakeExecutor) -> Callable[[str], Any]:
    def _get(b: str) -> Any:
        return executor

    return _get


@dataclass
class ChipRecorder:
    """Capture chip emits. Records (phase, payload) in the order called."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    parent_conv_id: str | None = None
    child_conv_id: str | None = None
    delegation_id: str | None = None
    target_folder: str | None = None
    target_name: str | None = None
    raise_exc: BaseException | None = None

    async def __call__(
        self,
        *,
        parent_conv_id: str,
        child_conv_id: str,
        delegation_id: str,
        target_folder: str,
        target_name: str,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        # Stash the identity fields on the first call (open) for
        # cross-call invariants. They must NEVER change for the rest
        # of the delegation — that would mean the emitter cross-wired
        # to a different delegation's child conv.
        if self.parent_conv_id is None:
            self.parent_conv_id = parent_conv_id
            self.child_conv_id = child_conv_id
            self.delegation_id = delegation_id
            self.target_folder = target_folder
            self.target_name = target_name
        else:
            assert parent_conv_id == self.parent_conv_id
            assert child_conv_id == self.child_conv_id
            assert delegation_id == self.delegation_id
        self.calls.append((phase, payload))
        if self.raise_exc is not None:
            raise self.raise_exc


async def _drain() -> None:
    """Yield enough ticks for all in-flight create_task chip emits to land.

    The handler schedules chip emits via ``asyncio.create_task`` so the
    audit path doesn't block. In tests we need a few yields after the
    handler returns to let those tasks run. A short ``sleep(0)`` loop
    is enough — emits don't await on anything real (the emitter is a
    pure recorder)."""
    for _ in range(20):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Seeding (web parent so the chip emitter has somewhere to send to,
# even though we use the mock emitter and bypass the actual gateway)
# ---------------------------------------------------------------------------


@dataclass
class _Seeded:
    tenant_id: str
    frontdesk: Coworker
    target: Coworker
    parent_conv_id: str
    state: OrchestratorState
    queue: GroupQueue


async def _seed() -> _Seeded:
    tenant = await create_tenant(
        name=f"World-{uuid.uuid4().hex[:6]}",
        slug=f"world-{uuid.uuid4().hex[:8]}",
    )
    frontdesk = await create_coworker(
        tenant_id=tenant.id,
        name="Frontdesk",
        folder=f"frontdesk-{uuid.uuid4().hex[:8]}",
        agent_role="super_agent",
        is_frontdesk=True,
    )
    target = await create_coworker(
        tenant_id=tenant.id,
        name="Trading",
        folder=f"trading-{uuid.uuid4().hex[:8]}",
        agent_role="agent",
    )
    parent_binding = await create_channel_binding(
        coworker_id=frontdesk.id,
        tenant_id=tenant.id,
        channel_type="web",
        credentials={},
    )
    parent_conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=frontdesk.id,
        channel_binding_id=parent_binding.id,
        channel_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
        requires_trigger=False,
    )
    state = OrchestratorState()
    state.coworkers[frontdesk.id] = CoworkerState.from_coworker(frontdesk)
    state.coworkers[target.id] = CoworkerState.from_coworker(target)
    queue = GroupQueue(transport=None, runtime=None, orchestrator_state=state)
    return _Seeded(
        tenant_id=tenant.id, frontdesk=frontdesk, target=target,
        parent_conv_id=parent_conv.id, state=state, queue=queue,
    )


def _payload(s: _Seeded) -> dict[str, Any]:
    return {
        "type": "delegate_to_agent",
        "tenantId": s.tenant_id,
        "fromCoworkerId": s.frontdesk.id,
        "fromConversationId": s.parent_conv_id,
        "userId": None,
        "target": s.target.folder,
        "prompt": "do the thing",
        "contextMode": "isolated",
        "depth": 0,
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_happy_path_emits_open_then_close_success() -> None:
    s = await _seed()
    rec = ChipRecorder()
    text = AgentOutput(
        status="success", result="ok", is_final=False,
    )
    marker = AgentOutput(
        status="success", result=None, new_session_id="S1", is_final=True,
    )
    executor = FakeExecutor(events=[text, marker])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=rec,
    )
    await _drain()
    phases = [c[0] for c in rec.calls]
    assert phases[0] == "open"
    assert phases[-1] == "close"
    # The close payload must reflect the merged audit status (success)
    # and carry a non-negative duration. duration_ms == 0 is allowed in
    # tests because everything's mocked and fast — clamp the assert to
    # >= 0 so this test is not flaky on a fast machine.
    close_payload = rec.calls[-1][1]
    assert close_payload["final_status"] == "success"
    assert close_payload["duration_ms"] >= 0
    # Open payload must include the context_mode so the dev-only tag
    # in the UI has something to render.
    open_payload = rec.calls[0][1]
    assert open_payload["context_mode"] == "isolated"


async def test_tool_use_event_translated_to_chip_emit() -> None:
    s = await _seed()
    rec = ChipRecorder()
    tool_event = AgentOutput(
        status="tool_use",
        result=None,
        metadata={"tool": "mcp__rolemesh__send_message", "input": "(...)"},
    )
    marker = AgentOutput(
        status="success", result="ok", is_final=True,
    )
    executor = FakeExecutor(events=[tool_event, marker])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=rec,
    )
    await _drain()
    phases = [c[0] for c in rec.calls]
    assert "tool_use" in phases
    # Find the tool_use payload and verify the raw tool name was passed
    # through (frontend beautifies it, backend should NOT).
    tool_payload = next(p for ph, p in rec.calls if ph == "tool_use")
    assert tool_payload["tool_name"] == "mcp__rolemesh__send_message"
    assert tool_payload["tool_input"] == "(...)"


async def test_safety_blocked_emits_close_with_safety_blocked() -> None:
    s = await _seed()
    rec = ChipRecorder()
    safety = AgentOutput(
        status="safety_blocked",
        result="declined",
        metadata={"stage": "model_output"},
    )
    executor = FakeExecutor(events=[safety])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=rec,
    )
    await _drain()
    close_payload = rec.calls[-1][1]
    assert rec.calls[-1][0] == "close"
    assert close_payload["final_status"] == "safety_blocked"


async def test_closure_exception_emits_close_with_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closure that raises a non-timeout exception must still emit
    close(error). Otherwise the parent UI would strand the sub-chip."""
    s = await _seed()
    rec = ChipRecorder()
    executor = FakeExecutor(raise_exc=RuntimeError("backend boom"))
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=rec,
    )
    await _drain()
    phases = [c[0] for c in rec.calls]
    assert phases[0] == "open"
    assert phases[-1] == "close"
    assert rec.calls[-1][1]["final_status"] == "error"


async def test_business_timeout_emits_close_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the closure exceeds the business deadline, the close payload
    must report final_status="timeout" — distinct from "error" — so
    Ops can tell deadline trips from real backend errors."""
    # Compress the deadline from 300s to ~50ms so the test stays fast.
    monkeypatch.setattr(
        "rolemesh.orchestration.delegation.DEFAULT_BUSINESS_DEADLINE_S",
        0.05,
    )
    s = await _seed()
    rec = ChipRecorder()
    # Sleep long enough to trip the inner wait_for, never emit anything.
    executor = FakeExecutor(initial_sleep=0.5)
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=rec,
    )
    await _drain()
    assert rec.calls[-1][0] == "close"
    assert rec.calls[-1][1]["final_status"] == "timeout"


async def test_none_emit_chip_event_does_not_crash_handler() -> None:
    """The chip emitter is optional. Passing None must work — used by
    tests, by non-web parents, and by deployments that haven't wired
    the gateway yet. Regression guard: an accidental ``await
    emit_chip_event(...)`` without the None check would NPE every
    delegation in those deployments."""
    s = await _seed()
    marker = AgentOutput(status="success", result="ok", is_final=True)
    executor = FakeExecutor(events=[marker])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=None,
    )
    response = json.loads(msg.replies[0])
    assert response["status"] == "success"


async def test_emitter_raising_does_not_break_audit_path() -> None:
    """If the emitter itself raises (gateway down, NATS hiccup), the
    handler MUST still complete normally — the audit row is the
    authoritative record, not the chip stream."""
    s = await _seed()
    rec = ChipRecorder(raise_exc=RuntimeError("emit broke"))
    marker = AgentOutput(status="success", result="ok", is_final=True)
    executor = FakeExecutor(events=[marker])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_executor_factory(executor),
        emit_chip_event=rec,
    )
    await _drain()
    # Response should still report success — the chip emit failure must
    # not leak into the audit-tracked response.
    response = json.loads(msg.replies[0])
    assert response["status"] == "success"
    assert response["isError"] is False
