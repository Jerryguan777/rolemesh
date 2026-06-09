"""End-to-end tests for the delegation handler (handbook §6 Step 5.5).

Covers the DB-touching scenarios from the 23-test matrix:

  #1  happy path
  #4  cross-tenant
  #5  target not found (incl. UUID fallback case)
  #6  depth limit literal "max 1 hop" mutation guard (handler-side)
  #8  isolated mode (session_id=None, fresh child per call)
  #9  sticky round-trip + A3 (isolated-then-sticky must NOT reuse)
  #10 safety_blocked passthrough
  #11 business deadline → audit status='timeout' + "took too long"
  #12 audit idempotency (timeout-then-success no-ops)
  #14 parallel delegation in one turn (distinct queue_keys)
  #17b role_config flows through AgentInitData serialization
  #18 GroupQueue shutting down — no enqueue, dedicated error message
  #19 child conv NOT in _state.coworkers
  #20 sticky concurrency race — exactly one child row, one session
  #21 queue.request_shutdown called on every terminal path
  #22 OUTER_GUARD vs business timeout audit messages must differ
  #23 startup cleanup of stale running rows runs BEFORE subscribe

The pure-Python scenarios (#2, #3, #7, #13a-d, #16, #17a) live in
``test_delegate_unit.py`` — they don't need DB or a stub executor.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from rolemesh.agent.executor import AgentInput, AgentOutput
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.container.scheduler import GroupQueue
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.db import (
    cleanup_running_delegations,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    create_user,
    get_session,
    insert_delegation,
    update_delegation_terminal,
)
from rolemesh.db._pool import admin_conn
from rolemesh.ipc.protocol import AgentInitData
from rolemesh.orchestration import delegation as deleg_mod
from rolemesh.orchestration.delegation import handle_delegate_request

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.core.types import Coworker

pytestmark = pytest.mark.usefixtures("test_db")


# ---------------------------------------------------------------------------
# FakeMsg / FakeExecutor harness
# ---------------------------------------------------------------------------


@dataclass
class FakeMsg:
    data: bytes
    replies: list[bytes] = field(default_factory=list)

    async def respond(self, body: bytes) -> None:
        self.replies.append(body)


@dataclass
class FakeExecutor:
    """Scripted executor — emits AgentOutput events through on_output.

    ``events`` is the list to emit, in order. ``pre_emit_delay`` is the
    per-event sleep so tests of the business-deadline path can simulate
    "LLM still thinking" without a real container.

    ``call_on_process``: if False, do NOT call ``on_process`` —
    simulates a backend that exits before registering. The handler's
    request_shutdown is a no-op for unregistered processes (since
    ``state.job_id`` is None), but the closure must still drive
    ``_on_output`` to a terminal event for the handler to release.
    """

    events: list[AgentOutput] = field(default_factory=list)
    pre_emit_delay: float = 0.0
    call_on_process: bool = True
    raise_exc: BaseException | None = None
    # If set, sleeps this long BEFORE doing anything (used to drive
    # the business-deadline path: with DEFAULT_BUSINESS_DEADLINE_S
    # monkey-patched down to 0.05s, a 0.5s pre-sleep guarantees a
    # business-timeout TimeoutError inside the inner wait_for.
    initial_sleep: float = 0.0

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


def _capturing_executor_factory(
    backend: str, executor: FakeExecutor,
) -> Callable[[str], Any]:
    captured = {"backend": backend, "executor": executor}

    def _get(b: str) -> Any:
        return captured["executor"]

    return _get


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


@dataclass
class _Seeded:
    tenant_id: str
    frontdesk: Coworker
    target: Coworker
    parent_conv_id: str
    state: OrchestratorState
    queue: GroupQueue


async def _seed_world(
    *,
    target_kwargs: dict[str, Any] | None = None,
    frontdesk_kwargs: dict[str, Any] | None = None,
) -> _Seeded:
    """Build a tenant + frontdesk + active specialist + parent conv.

    State is loaded fresh; the GroupQueue is constructed with
    ``transport=None``/``runtime=None`` so ``request_shutdown`` is a
    no-op in tests (we mock it explicitly when assertions need it).
    """
    tenant = await create_tenant(
        name=f"World-{uuid.uuid4().hex[:6]}",
        slug=f"world-{uuid.uuid4().hex[:8]}",
    )
    # A frontdesk must hold the agent_delegate permission. Tests that
    # need a gated frontdesk override ``permissions`` via frontdesk_kwargs.
    fd_kwargs: dict[str, Any] = {
        "permissions": AgentPermissions(agent_delegate=True),
        **(frontdesk_kwargs or {}),
    }
    frontdesk = await create_coworker(
        tenant_id=tenant.id,
        name="Frontdesk",
        folder=f"frontdesk-{uuid.uuid4().hex[:8]}",
        is_frontdesk=True,
        **fd_kwargs,
    )
    target = await create_coworker(
        tenant_id=tenant.id,
        name="Trading",
        folder=f"trading-{uuid.uuid4().hex[:8]}",
        **(target_kwargs or {}),
    )
    parent_binding = await create_channel_binding(
        coworker_id=frontdesk.id,
        tenant_id=tenant.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    parent_conv = await create_conversation(
        tenant_id=tenant.id,
        coworker_id=frontdesk.id,
        channel_binding_id=parent_binding.id,
        channel_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
    )

    state = OrchestratorState()
    state.coworkers[frontdesk.id] = CoworkerState.from_coworker(frontdesk)
    state.coworkers[target.id] = CoworkerState.from_coworker(target)
    queue = GroupQueue(transport=None, runtime=None, orchestrator_state=state)
    return _Seeded(
        tenant_id=tenant.id, frontdesk=frontdesk, target=target,
        parent_conv_id=parent_conv.id, state=state, queue=queue,
    )


def _payload(s: _Seeded, *, target_slug: str | None = None,
             prompt: str = "Hello", context_mode: str = "isolated",
             depth: int = 0, user_id: str | None = None,
             override_tenant_id: str | None = None) -> dict[str, Any]:
    return {
        "type": "delegate_to_agent",
        "tenantId": override_tenant_id or s.tenant_id,
        "fromCoworkerId": s.frontdesk.id,
        "fromConversationId": s.parent_conv_id,
        "userId": user_id,
        "target": target_slug or s.target.folder,
        "prompt": prompt,
        "contextMode": context_mode,
        "depth": depth,
    }


async def _audit_row(delegation_id: str, tenant_id: str) -> dict[str, Any]:
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM delegations WHERE id = $1::uuid", delegation_id,
        )
    assert row is not None, delegation_id
    return dict(row)


async def _all_audit_rows(tenant_id: str) -> list[dict[str, Any]]:
    async with admin_conn() as conn:
        rows = await conn.fetch(
            "SELECT * FROM delegations WHERE tenant_id = $1::uuid "
            "ORDER BY started_at",
            tenant_id,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Test #1 — happy path
# ---------------------------------------------------------------------------


async def test_happy_path_returns_success_and_creates_audit_row() -> None:
    """Pi two-event success (text + marker) through the full handler.

    Asserts: child conv created with parent_conversation_id set (the
    child-exclusion invariant is now enforced by the
    ``parent_conversation_id IS NULL`` list filter — see
    tests/core/test_loader_excludes_children.py — since the legacy
    ``requires_trigger`` column was removed); audit row in
    status='success'; response carries metadata + isError=false."""
    s = await _seed_world()
    text = AgentOutput(
        status="success", result="Trading done", is_final=False,
    )
    marker = AgentOutput(
        status="success", result=None,
        new_session_id="S1", is_final=True,
    )
    executor = FakeExecutor(events=[text, marker])

    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert len(msg.replies) == 1
    response = json.loads(msg.replies[0])
    assert response["status"] == "success"
    assert response["text"] == "Trading done"
    assert response["isError"] is False
    metadata = response["metadata"]
    assert metadata["newSessionId"] == "S1"
    assert metadata["targetFolder"] == s.target.folder
    assert metadata["childConversationId"]

    # Audit row + child conv invariants
    rows = await _all_audit_rows(s.tenant_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["from_coworker_id"] == uuid.UUID(s.frontdesk.id)
    assert rows[0]["target_coworker_id"] == uuid.UUID(s.target.id)

    # Child conv must carry parent_conversation_id — that link is now the
    # sole mechanism that keeps the child out of the orchestrator's loader
    # (the ``parent_conversation_id IS NULL`` list filter), replacing the
    # removed ``requires_trigger`` column (handbook §6 Step 2.5; guarded by
    # tests/core/test_loader_excludes_children.py).
    child_id = metadata["childConversationId"]
    async with admin_conn() as conn:
        child_row = await conn.fetchrow(
            "SELECT parent_conversation_id "
            "FROM conversations WHERE id = $1::uuid",
            child_id,
        )
    assert child_row is not None
    assert str(child_row["parent_conversation_id"]) == s.parent_conv_id


# ---------------------------------------------------------------------------
# Test #4 — cross-tenant rejected
# ---------------------------------------------------------------------------


async def test_cross_tenant_rejected_with_tenant_mismatch() -> None:
    """The handler must NOT trust the payload's ``tenantId`` blindly: if
    a frontdesk's recorded tenant differs from the payload's tenant,
    we refuse. This catches a forged ``tenantId`` claim from the
    agent-runner side."""
    s = await _seed_world()
    # Send a payload whose tenant doesn't match the frontdesk's tenant.
    other_tenant = str(uuid.uuid4())
    msg = FakeMsg(
        data=json.dumps(_payload(s, override_tenant_id=other_tenant)).encode()
    )
    executor = FakeExecutor()
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is True
    assert "Tenant mismatch" in response["text"]

    # No audit row should have been inserted.
    assert await _all_audit_rows(s.tenant_id) == []
    assert await _all_audit_rows(other_tenant) == []


# ---------------------------------------------------------------------------
# Test #5 — target not found (wrong folder + wrong UUID)
# ---------------------------------------------------------------------------


async def test_target_not_found_response_contains_catalog() -> None:
    s = await _seed_world()
    msg = FakeMsg(
        data=json.dumps(_payload(s, target_slug="does-not-exist")).encode()
    )
    executor = FakeExecutor()
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is True
    # Error text should include both "Agent 'X' not found" AND the
    # rendered catalog so the LLM can self-correct.
    assert "does-not-exist" in response["text"]
    assert "Trading" in response["text"]  # catalog body
    assert "(id:" in response["text"]


async def test_target_not_found_when_passed_wrong_uuid() -> None:
    """Test #5 sub-case (UUID fallback miss): passing a syntactically
    valid UUID that doesn't match any coworker still surfaces
    "not found" with a catalog. Important because _resolve_target
    tries folder first then UUID — a future PR could break the UUID
    fallback such that wrong UUIDs DO match the folder path silently.
    Either way, the wire-level surface must say "not found + catalog"."""
    s = await _seed_world()
    fake = str(uuid.uuid4())
    msg = FakeMsg(
        data=json.dumps(_payload(s, target_slug=fake)).encode()
    )
    executor = FakeExecutor()
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is True
    assert fake in response["text"]
    assert "Trading" in response["text"]


# ---------------------------------------------------------------------------
# Test #6 — depth limit (handler-side, "max 1 hop" mutation guard)
# ---------------------------------------------------------------------------


async def test_depth_limit_rejects_depth_1_with_literal_max_one_hop() -> None:
    """Mutation guard: assert the literal "max 1 hop" string. A bare
    ``isError`` check would silently pass if MAX_DELEGATION_DEPTH
    flipped from 1 to 2 because depth=1 would then be accepted."""
    s = await _seed_world()
    msg = FakeMsg(data=json.dumps(_payload(s, depth=1)).encode())
    executor = FakeExecutor()
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is True
    assert "max 1 hop" in response["text"]


async def test_depth_zero_is_allowed() -> None:
    """Negative control for #6 — the frontdesk's initial call carries
    depth=0 and MUST be allowed."""
    s = await _seed_world()
    executor = FakeExecutor(events=[
        AgentOutput(
            status="success", result="ok", new_session_id="S", is_final=True,
        ),
    ])
    msg = FakeMsg(data=json.dumps(_payload(s, depth=0)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is False


# ---------------------------------------------------------------------------
# Test #8 — isolated mode (new child conv every call, session_id=None)
# ---------------------------------------------------------------------------


async def test_isolated_mode_creates_new_child_conv_every_call() -> None:
    """Isolated calls carry a UUID-suffixed channel_chat_id; each call
    must land on a fresh child conv. Pre-existing sessions on a
    sticky child must NOT leak into an isolated call."""
    s = await _seed_world()
    executor = FakeExecutor(events=[
        AgentOutput(status="success", result="r", is_final=True),
    ])
    captured_session_ids: list[str | None] = []

    real_execute = executor.execute

    async def _spy_execute(
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        captured_session_ids.append(inp.session_id)
        return await real_execute(inp, on_process, on_output)

    executor.execute = _spy_execute  # type: ignore[method-assign]

    # Two isolated calls
    msg1 = FakeMsg(data=json.dumps(_payload(s, context_mode="isolated")).encode())
    msg2 = FakeMsg(data=json.dumps(_payload(s, context_mode="isolated")).encode())
    await handle_delegate_request(
        msg1, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    await handle_delegate_request(
        msg2, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    r1 = json.loads(msg1.replies[0])
    r2 = json.loads(msg2.replies[0])
    # Two DISTINCT child conv ids — UUID suffix prevents collision.
    assert r1["metadata"]["childConversationId"] != r2["metadata"]["childConversationId"]
    # No session_id flows in for either call (isolated mode).
    assert captured_session_ids == [None, None]


# ---------------------------------------------------------------------------
# Test #9 — sticky round-trip + A3 regression
# ---------------------------------------------------------------------------


async def test_sticky_round_trip_persists_session_and_reuses_child() -> None:
    """First sticky call: handler MUST explicitly call set_session for
    the CHILD conv (not the parent — that would be a real bug since
    the parent runs on a different coworker, frontdesk). Second sticky
    call: same child conv reused, prior session_id flows in.

    Mutation guard: also assert the parent's session row is unchanged
    so a regression where ``set_session(parent_conv.id, ...)`` is
    called by accident fails this test.
    """
    s = await _seed_world()
    captured_session_ids: list[str | None] = []

    success_with_sid = AgentOutput(
        status="success", result="first",
        new_session_id="S-trade-1", is_final=True,
    )
    executor = FakeExecutor(events=[success_with_sid])
    real_execute = executor.execute

    async def _spy_execute(
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        captured_session_ids.append(inp.session_id)
        return await real_execute(inp, on_process, on_output)

    executor.execute = _spy_execute  # type: ignore[method-assign]

    # Establish baseline: parent has no session before delegation.
    parent_session_before = await get_session(
        s.parent_conv_id, tenant_id=s.tenant_id,
    )

    # First sticky call
    msg1 = FakeMsg(data=json.dumps(_payload(s, context_mode="sticky")).encode())
    await handle_delegate_request(
        msg1, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    r1 = json.loads(msg1.replies[0])
    assert r1["isError"] is False
    child_id_first = r1["metadata"]["childConversationId"]
    assert r1["metadata"]["newSessionId"] == "S-trade-1"

    # Handler MUST have written set_session(child_id, S-trade-1)
    saved = await get_session(child_id_first, tenant_id=s.tenant_id)
    assert saved == "S-trade-1"

    # Mutation guard: parent's session row must be untouched.
    parent_session_after = await get_session(
        s.parent_conv_id, tenant_id=s.tenant_id,
    )
    assert parent_session_after == parent_session_before, (
        "set_session must target child_conv.id, NOT parent_conv.id"
    )

    # Second sticky call — child must be reused, S-trade-1 flows in.
    executor.events = [
        AgentOutput(
            status="success", result="second",
            new_session_id="S-trade-2", is_final=True,
        ),
    ]
    msg2 = FakeMsg(data=json.dumps(_payload(s, context_mode="sticky")).encode())
    await handle_delegate_request(
        msg2, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    r2 = json.loads(msg2.replies[0])
    assert r2["metadata"]["childConversationId"] == child_id_first
    assert captured_session_ids == [None, "S-trade-1"]


async def test_a3_isolated_then_sticky_does_not_reuse_child() -> None:
    """A3 regression (handbook test #9): an isolated call CREATES a
    UUID-suffixed child; a follow-up sticky call must NOT pick up that
    isolated child. The exact channel_chat_id match in
    ``find_child_conversation`` is the mechanism."""
    s = await _seed_world()
    executor = FakeExecutor(events=[
        AgentOutput(
            status="success", result="r",
            new_session_id="S", is_final=True,
        ),
    ])
    msg_iso = FakeMsg(data=json.dumps(_payload(s, context_mode="isolated")).encode())
    await handle_delegate_request(
        msg_iso, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    r_iso = json.loads(msg_iso.replies[0])
    iso_child = r_iso["metadata"]["childConversationId"]

    msg_sticky = FakeMsg(data=json.dumps(_payload(s, context_mode="sticky")).encode())
    await handle_delegate_request(
        msg_sticky, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    r_sticky = json.loads(msg_sticky.replies[0])
    sticky_child = r_sticky["metadata"]["childConversationId"]
    assert iso_child != sticky_child


# ---------------------------------------------------------------------------
# Test #10 — safety_blocked passthrough
# ---------------------------------------------------------------------------


async def test_safety_blocked_passthrough() -> None:
    s = await _seed_world()
    executor = FakeExecutor(events=[
        AgentOutput(
            status="safety_blocked",
            result="disallowed_topic",
            metadata={"stage": "MODEL_OUTPUT"},
            is_final=True,
        ),
    ])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["status"] == "safety_blocked"
    assert response["isError"] is True
    # The literal reason text MUST survive intact — failure-passthrough
    # contract (handbook §4 #23).
    assert "disallowed_topic" in response["text"]
    assert response["metadata"]["safetyStage"] == "MODEL_OUTPUT"

    rows = await _all_audit_rows(s.tenant_id)
    assert rows[-1]["status"] == "safety_blocked"


# ---------------------------------------------------------------------------
# Test #11 — business deadline trips → audit timeout
# ---------------------------------------------------------------------------


async def test_business_deadline_trips_audit_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inner ``wait_for(execute, DEFAULT_BUSINESS_DEADLINE_S)`` trips.

    Monkey-patches the deadline to 0.05s and has the executor sleep
    longer; the closure surfaces a TimeoutError through
    ``result_future.set_exception``, which the handler catches in the
    ``except TimeoutError`` branch (NOT the OUTER_GUARD branch — that's
    test #22's distinction).
    """
    monkeypatch.setattr(deleg_mod, "DEFAULT_BUSINESS_DEADLINE_S", 0.05)
    s = await _seed_world()
    executor = FakeExecutor(events=[], initial_sleep=0.5)  # sleeps past deadline
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["status"] == "timeout"
    assert response["isError"] is True
    assert "took too long" in response["text"]

    rows = await _all_audit_rows(s.tenant_id)
    assert rows[-1]["status"] == "timeout"
    assert "took too long" in (rows[-1]["error_message"] or "")


# ---------------------------------------------------------------------------
# Test #12 — audit idempotency (terminal status never overwritten)
# ---------------------------------------------------------------------------


async def test_audit_idempotency_terminal_never_overwritten() -> None:
    """Conditional UPDATE: a late event MUST not flip a finished row.

    Mutation guard: assert BOTH the bool return (False the 2nd time)
    AND the row's final status (still 'timeout'). A test that only
    asserted the bool would silently pass under removal of the
    ``WHERE status='running'`` clause — the row would be re-written
    to 'success' but the bool would be True.
    """
    s = await _seed_world()
    delegation_id = await insert_delegation(
        tenant_id=s.tenant_id,
        parent_conversation_id=s.parent_conv_id,
        child_conversation_id=s.parent_conv_id,  # placeholder; unique constraint not enforced here
        from_coworker_id=s.frontdesk.id,
        target_coworker_id=s.target.id,
        user_id=None,
        prompt_sha256="x" * 64,
        context_mode="isolated",
    )
    # First terminal: timeout
    first = await update_delegation_terminal(
        delegation_id, tenant_id=s.tenant_id,
        status="timeout", duration_ms=100,
        error_message="took too long",
    )
    assert first is True

    # Second terminal: attempt to overwrite with success
    second = await update_delegation_terminal(
        delegation_id, tenant_id=s.tenant_id,
        status="success", duration_ms=200,
        error_message=None,
    )
    assert second is False

    row = await _audit_row(delegation_id, s.tenant_id)
    assert row["status"] == "timeout"
    assert row["duration_ms"] == 100
    assert row["error_message"] == "took too long"


# ---------------------------------------------------------------------------
# Test #14 — parallel delegation
# ---------------------------------------------------------------------------


async def test_parallel_delegations_distinct_queue_keys_complete_concurrently() -> None:
    """Two delegations in the same turn to different targets must
    progress in parallel. The handler uses ``queue_key =
    'delegate:{child_id}'`` per call, and since each isolated call
    produces a distinct child id, the queue treats them as
    independent groups (handbook §3 fact 8).
    """
    s = await _seed_world()
    second_target = await create_coworker(
        tenant_id=s.tenant_id,
        name="Portfolio",
        folder=f"portfolio-{uuid.uuid4().hex[:8]}",
    )
    s.state.coworkers[second_target.id] = CoworkerState.from_coworker(second_target)

    exec_a = FakeExecutor(events=[
        AgentOutput(status="success", result="A done", is_final=True),
    ], pre_emit_delay=0.05)
    exec_b = FakeExecutor(events=[
        AgentOutput(status="success", result="B done", is_final=True),
    ], pre_emit_delay=0.05)

    def _get_executor(backend: str) -> Any:
        return exec_a  # same for both; isolation is on queue_key

    # Use distinct executors per call via separate get_executor closures.
    msg_a = FakeMsg(data=json.dumps(_payload(s, target_slug=s.target.folder)).encode())
    msg_b = FakeMsg(data=json.dumps(_payload(s, target_slug=second_target.folder)).encode())

    async def _run_with(executor: FakeExecutor, msg: FakeMsg) -> None:
        await handle_delegate_request(
            msg, state=s.state, queue=s.queue,
            get_executor=lambda b, e=executor: e,
        )

    await asyncio.gather(
        _run_with(exec_a, msg_a),
        _run_with(exec_b, msg_b),
    )
    ra = json.loads(msg_a.replies[0])
    rb = json.loads(msg_b.replies[0])
    assert ra["text"] == "A done"
    assert rb["text"] == "B done"

    rows = await _all_audit_rows(s.tenant_id)
    assert len([r for r in rows if r["status"] == "success"]) == 2


# ---------------------------------------------------------------------------
# Test #17b — role_config survives AgentInitData serialization
# ---------------------------------------------------------------------------


async def test_role_config_survives_agent_init_data_round_trip() -> None:
    """The agent-runner container reads ``AgentInitData`` from the
    JetStream KV bucket ``agent-init`` (see
    src/rolemesh/agent/container_executor.py:365-385 and
    src/agent_runner/main.py:519-521). A full integration test would
    need a real NATS+JS bucket; instead we exercise the
    serialize/deserialize path that the wire actually walks, with the
    AgentInput captured from the handler.

    GREP-AUDITED LOCATION: ``AgentInitData`` is put to KV bucket
    ``agent-init`` at container_executor.py:365 and read at
    agent_runner/main.py:519. The wire payload is its
    ``serialize`` output — that's what we exercise here.
    """
    s = await _seed_world()
    captured: dict[str, AgentInput] = {}
    executor = FakeExecutor(events=[
        AgentOutput(
            status="success", result="ok", new_session_id="S", is_final=True,
        ),
    ])

    async def _spy_execute(
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        captured["input"] = inp
        return await FakeExecutor.execute(executor, inp, on_process, on_output)

    executor.execute = _spy_execute  # type: ignore[method-assign]
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )

    agent_input = captured["input"]
    # Build the AgentInitData the ContainerAgentExecutor would build
    # at line 365-385, then round-trip through the wire format.
    init = AgentInitData(
        prompt=agent_input.prompt,
        group_folder=agent_input.group_folder,
        chat_jid=agent_input.chat_jid,
        permissions=agent_input.permissions,
        tenant_id=agent_input.tenant_id,
        coworker_id=agent_input.coworker_id,
        conversation_id=agent_input.conversation_id,
        user_id=agent_input.user_id,
        session_id=agent_input.session_id,
        is_scheduled_task=agent_input.is_scheduled_task,
        assistant_name=agent_input.assistant_name,
        system_prompt=agent_input.system_prompt,
        role_config=agent_input.role_config,
        mcp_servers=None,
        approval_policies=None,
        safety_rules=None,
        slow_check_specs=None,
    )
    payload_bytes = init.serialize()
    rebuilt = AgentInitData.deserialize(payload_bytes)
    assert rebuilt.role_config == {
        "is_delegated_call": True,
        "delegated_by": s.frontdesk.id,
        "delegation_depth": 1,
        "parent_conversation_id": s.parent_conv_id,
        "delegation_id": rebuilt.role_config["delegation_id"],  # type: ignore[index]
    }


# ---------------------------------------------------------------------------
# Test #18 — GroupQueue shutting down
# ---------------------------------------------------------------------------


async def test_group_queue_shutting_down_refuses_before_enqueue() -> None:
    """Handler MUST detect _shutting_down BEFORE calling enqueue_task.
    Mocked enqueue_task asserts zero calls; audit row carries the
    literal contracted message so ops alerting can match on it."""
    s = await _seed_world()
    s.queue._shutting_down = True
    enqueue_calls: list[Any] = []
    real_enqueue = s.queue.enqueue_task

    def _spy_enqueue(*args: Any, **kw: Any) -> None:
        enqueue_calls.append((args, kw))
        return real_enqueue(*args, **kw)

    s.queue.enqueue_task = _spy_enqueue  # type: ignore[method-assign]
    executor = FakeExecutor()
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert enqueue_calls == []  # MUST be empty — early refusal
    response = json.loads(msg.replies[0])
    assert response["isError"] is True
    assert response["text"] == "GroupQueue is shutting down; delegation refused."

    rows = await _all_audit_rows(s.tenant_id)
    assert rows[-1]["status"] == "error"
    assert rows[-1]["error_message"] == "GroupQueue is shutting down; delegation refused."


# ---------------------------------------------------------------------------
# Test #19 — child conv NOT in _state.coworkers
# ---------------------------------------------------------------------------


async def test_child_conv_not_in_state_after_delegation() -> None:
    """Handler never adds child convs to ``_state.coworkers[*]
    .conversations``. The orchestrator's loader is the only legitimate
    path into that dict and it filters out child convs by default.

    Pin the invariant from the delegation side too so a future PR
    that mistakenly populates ``cs.conversations[child.id] = ...``
    from the handler fails immediately.
    """
    s = await _seed_world()
    executor = FakeExecutor(events=[
        AgentOutput(status="success", result="r", is_final=True),
    ])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    child_id = response["metadata"]["childConversationId"]
    for cs in s.state.coworkers.values():
        assert child_id not in cs.conversations


# ---------------------------------------------------------------------------
# Test #20 — sticky concurrency race
# ---------------------------------------------------------------------------


async def test_sticky_concurrent_calls_converge_to_one_child_and_one_session() -> None:
    """``asyncio.gather`` two sticky calls. Both must succeed; exactly
    one child conv row exists; both calls reuse the same session
    after the first persists it.

    Without ``INSERT … ON CONFLICT DO NOTHING RETURNING`` + the
    fallback SELECT in ``create_child_conversation``, the second
    caller would either crash on the UNIQUE violation or get None
    and proceed with a NULL child id — both regressions.
    """
    s = await _seed_world()
    executor_a = FakeExecutor(events=[
        AgentOutput(
            status="success", result="a", new_session_id="S-race",
            is_final=True,
        ),
    ])
    executor_b = FakeExecutor(events=[
        AgentOutput(
            status="success", result="b", new_session_id="S-race",
            is_final=True,
        ),
    ])

    msg_a = FakeMsg(data=json.dumps(_payload(s, context_mode="sticky")).encode())
    msg_b = FakeMsg(data=json.dumps(_payload(s, context_mode="sticky")).encode())

    async def _run(executor: FakeExecutor, msg: FakeMsg) -> None:
        await handle_delegate_request(
            msg, state=s.state, queue=s.queue,
            get_executor=lambda b, e=executor: e,
        )

    await asyncio.gather(_run(executor_a, msg_a), _run(executor_b, msg_b))
    r_a = json.loads(msg_a.replies[0])
    r_b = json.loads(msg_b.replies[0])
    assert r_a["isError"] is False
    assert r_b["isError"] is False
    # Same child id — sticky_chat_id collides on UNIQUE, second falls
    # through to SELECT and gets the row the first one inserted.
    assert r_a["metadata"]["childConversationId"] == r_b["metadata"]["childConversationId"]

    # Exactly one child conv with the sticky chat_id.
    sticky_chat_id = f"internal:{s.parent_conv_id}:{s.target.id}"
    async with admin_conn() as conn:
        rows = await conn.fetch(
            "SELECT id FROM conversations "
            "WHERE channel_chat_id = $1",
            sticky_chat_id,
        )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Test #21 — request_shutdown called on every terminal path
# ---------------------------------------------------------------------------


async def test_request_shutdown_called_on_success_path() -> None:
    s = await _seed_world()
    calls: list[str] = []

    def _spy(group_jid: str) -> None:
        calls.append(group_jid)

    s.queue.request_shutdown = _spy  # type: ignore[method-assign]
    executor = FakeExecutor(events=[
        AgentOutput(
            status="success", result="ok", new_session_id="S", is_final=True,
        ),
    ])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert len(calls) >= 1
    # Pin the queue_key shape — "delegate:{child_id}" — so a refactor
    # that drops the prefix doesn't slip past.
    assert all(k.startswith("delegate:") for k in calls)


async def test_request_shutdown_called_on_error_path() -> None:
    s = await _seed_world()
    calls: list[str] = []
    s.queue.request_shutdown = lambda jid: calls.append(jid)  # type: ignore[method-assign]
    executor = FakeExecutor(events=[
        AgentOutput(status="error", result=None, error="boom", is_final=True),
    ])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert len(calls) >= 1


async def test_request_shutdown_called_on_safety_blocked_path() -> None:
    s = await _seed_world()
    calls: list[str] = []
    s.queue.request_shutdown = lambda jid: calls.append(jid)  # type: ignore[method-assign]
    executor = FakeExecutor(events=[
        AgentOutput(
            status="safety_blocked", result="nope", is_final=True,
            metadata={"stage": "INPUT_PROMPT"},
        ),
    ])
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert len(calls) >= 1


async def test_request_shutdown_called_on_business_timeout_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deleg_mod, "DEFAULT_BUSINESS_DEADLINE_S", 0.05)
    s = await _seed_world()
    calls: list[str] = []
    s.queue.request_shutdown = lambda jid: calls.append(jid)  # type: ignore[method-assign]
    executor = FakeExecutor(events=[], initial_sleep=0.5)
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert len(calls) >= 1


# ---------------------------------------------------------------------------
# Test #22 — OUTER_GUARD vs business timeout — distinct audit
# ---------------------------------------------------------------------------


async def test_outer_guard_audit_message_differs_from_business_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Variant a: monkey-patch enqueue_task to no-op so the closure
    never starts; OUTER_GUARD fires after OUTER_GUARD_S seconds (we
    set it to 0.1s to keep the test fast).

    Variant b: closure runs but business deadline trips. Already
    exercised in test #11 — we re-assert here SPECIFICALLY that the
    two ``error_message`` strings differ, because that's what ops
    use to distinguish "queue stalled" from "slow LLM" in the
    runbook.
    """
    monkeypatch.setattr(deleg_mod, "OUTER_GUARD_S", 0.1)
    monkeypatch.setattr(deleg_mod, "DEFAULT_BUSINESS_DEADLINE_S", 0.05)

    # Variant a: closure never runs.
    s_a = await _seed_world()
    s_a.queue.enqueue_task = lambda *a, **kw: None  # type: ignore[method-assign]
    executor = FakeExecutor()
    msg_a = FakeMsg(data=json.dumps(_payload(s_a)).encode())
    await handle_delegate_request(
        msg_a, state=s_a.state, queue=s_a.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    rows_a = await _all_audit_rows(s_a.tenant_id)
    assert rows_a[-1]["status"] == "error"
    msg_a_audit = rows_a[-1]["error_message"]
    assert msg_a_audit == "Delegation task never started (queue stalled)."

    # Variant b: closure runs but slow LLM.
    s_b = await _seed_world()
    executor_b = FakeExecutor(events=[], initial_sleep=0.5)
    msg_b = FakeMsg(data=json.dumps(_payload(s_b)).encode())
    await handle_delegate_request(
        msg_b, state=s_b.state, queue=s_b.queue,
        get_executor=_capturing_executor_factory("claude", executor_b),
    )
    rows_b = await _all_audit_rows(s_b.tenant_id)
    assert rows_b[-1]["status"] == "timeout"
    msg_b_audit = rows_b[-1]["error_message"]
    assert "took too long" in (msg_b_audit or "")

    # Distinct strings.
    assert msg_a_audit != msg_b_audit


# ---------------------------------------------------------------------------
# Test #23 — startup cleanup runs BEFORE subscribe
# ---------------------------------------------------------------------------


async def test_cleanup_running_delegations_seals_stale_rows() -> None:
    """Pre-insert two rows in status='running' and call
    ``cleanup_running_delegations()``. Both should flip to 'error'
    with a non-empty error_message. This verifies the SQL works in
    isolation; the ORDERING invariant (cleanup BEFORE subscribe) is
    asserted in the next test."""
    s = await _seed_world()
    d1 = await insert_delegation(
        tenant_id=s.tenant_id,
        parent_conversation_id=s.parent_conv_id,
        child_conversation_id=s.parent_conv_id,
        from_coworker_id=s.frontdesk.id,
        target_coworker_id=s.target.id,
        user_id=None,
        prompt_sha256="a" * 64, context_mode="isolated",
    )
    d2 = await insert_delegation(
        tenant_id=s.tenant_id,
        parent_conversation_id=s.parent_conv_id,
        child_conversation_id=s.parent_conv_id,
        from_coworker_id=s.frontdesk.id,
        target_coworker_id=s.target.id,
        user_id=None,
        prompt_sha256="b" * 64, context_mode="sticky",
    )
    count = await cleanup_running_delegations()
    assert count >= 2

    r1 = await _audit_row(d1, s.tenant_id)
    r2 = await _audit_row(d2, s.tenant_id)
    assert r1["status"] == "error"
    assert r2["status"] == "error"
    assert (r1["error_message"] or "").startswith("cleanup: ")


async def test_cleanup_then_subscribe_ordering_invariant_holds_in_main() -> None:
    """Static check on src/rolemesh/main.py for the §6 Step 5.4 ordering
    invariant: ``cleanup_running_delegations()`` must appear before the
    ``agent.*.delegate.request`` subscribe call.

    A unit test that exercises only the SQL (the test above) would NOT
    catch a future refactor that moves the cleanup call AFTER the
    subscribe — but that refactor is exactly the kind of subtle
    regression the handbook flags as the reason this test exists.
    Asserting on the line ordering keeps the invariant pinned without
    needing to spin up a real orchestrator process.
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "rolemesh" / "main.py"
    text = src.read_text()
    cleanup_pos = text.find("cleanup_running_delegations")
    sub_pos = text.find("\"agent.*.delegate.request\"")
    assert cleanup_pos != -1, "cleanup_running_delegations call missing"
    assert sub_pos != -1, "delegate subscription missing"
    assert cleanup_pos < sub_pos, (
        "ORDERING INVARIANT VIOLATED: cleanup_running_delegations() must run "
        "BEFORE the agent.*.delegate.request subscriber is registered. "
        "Otherwise a delegate.request can arrive on a fresh orchestrator "
        "while stale 'running' audit rows from the prior crash are still "
        "in place — corrupting audit history."
    )


# ---------------------------------------------------------------------------
# Permission gate (handbook test #2 — orchestrator-side half)
# ---------------------------------------------------------------------------


async def test_orchestrator_side_perm_gate_when_caller_lacks_agent_delegate() -> None:
    """Test #2 orchestrator side: even if the agent's tool-side gate
    passes (e.g. a misbehaving runtime sends the RPC anyway), the
    orchestrator rejects when the caller's recorded permissions
    don't allow delegation. Defence-in-depth — handbook §8 #6."""
    s = await _seed_world(frontdesk_kwargs={
        # Inject permissions object lacking agent_delegate
        "permissions": AgentPermissions(
            task_schedule=True,
            task_manage_others=True,
            agent_delegate=False,
        ),
    })
    # Refresh the state's CoworkerState with the gated permissions.
    s.state.coworkers[s.frontdesk.id] = CoworkerState.from_coworker(s.frontdesk)
    executor = FakeExecutor()
    msg = FakeMsg(data=json.dumps(_payload(s)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is True
    assert "cannot delegate" in response["text"]


# ---------------------------------------------------------------------------
# Negative: handler safety net on bad payload
# ---------------------------------------------------------------------------


async def test_handler_never_raises_on_malformed_payload() -> None:
    """The top-of-handler safety net catches payload-parse errors so
    one bad JSON doesn't sink the responder. A raise that propagates
    out of the callback would silently break the NATS subscription
    until the orchestrator restarted — a very expensive failure mode
    for a typo in a single message."""
    s = await _seed_world()
    msg = FakeMsg(data=b"not even valid json")
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory(
            "claude", FakeExecutor(),
        ),
    )
    response = json.loads(msg.replies[0])
    assert response["isError"] is True


# ---------------------------------------------------------------------------
# OIDC pass-through (test #15)
# ---------------------------------------------------------------------------


async def test_oidc_user_id_flows_through_to_target_agent_input() -> None:
    """The parent's ``userId`` MUST land on the target's AgentInput.
    The X-RoleMesh-User-Id header that MCP egress injects is built
    from ``AgentInput.user_id``; without this field, downstream MCP
    calls would carry an empty / orchestrator-default identity and
    OIDC attribution would silently fail.
    """
    s = await _seed_world()
    captured: dict[str, AgentInput] = {}
    executor = FakeExecutor(events=[
        AgentOutput(status="success", result="r", is_final=True),
    ])
    real_execute = executor.execute

    async def _spy(
        inp: AgentInput,
        on_process: Callable[[str, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        captured["input"] = inp
        return await real_execute(inp, on_process, on_output)

    executor.execute = _spy  # type: ignore[method-assign]
    # Must be a real users row — child conv FKs into users(user_id).
    user = await create_user(tenant_id=s.tenant_id, name="oidc-user")
    msg = FakeMsg(data=json.dumps(_payload(s, user_id=user.id)).encode())
    await handle_delegate_request(
        msg, state=s.state, queue=s.queue,
        get_executor=_capturing_executor_factory("claude", executor),
    )
    assert captured["input"].user_id == user.id
