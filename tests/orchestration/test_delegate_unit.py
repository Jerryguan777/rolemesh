"""Pure-Python unit tests for the delegation helpers.

No DB, no executor, no NATS. Covers the handbook §6 Step 5.5 scenarios
that exercise validation paths and event-merge precedence:

  - #5  target not found (wrong folder AND wrong UUID)
  - #6  depth limit mutation guard (literal "max 1 hop")
  - #7  target is frontdesk / super_agent rejected
  - #3  self-delegation rejected
  - #13a-d  _merge_pi_two_event_pattern precedence across all four shapes
  - #17a  _build_agent_input role_config EXACT shape (no extra keys)
  - #16  send_message refused inside a delegated call
  - mutation-guard hooks for the static FRONTDESK_RULES + catalog (Commit 4)
    are in test_catalog_no_filesystem_terms.py — kept separate so this
    file can stay fast / no-state.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from agent_runner.tools.context import ToolContext
from agent_runner.tools.rolemesh_tools import send_message
from rolemesh.agent.executor import AgentOutput
from rolemesh.core.orchestrator_state import CoworkerState, OrchestratorState
from rolemesh.core.types import Coworker
from rolemesh.orchestration.delegation import (
    MAX_DELEGATION_DEPTH,
    OUTER_GUARD_S,
    _build_agent_input,
    _err,
    _map_output_to_response,
    _merge_pi_two_event_pattern,
    _resolve_target,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cw(**kw: object) -> Coworker:
    defaults: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "tenant_id": kw.pop("tenant_id"),
        "name": "Coworker",
        "folder": "coworker",
    }
    defaults.update(kw)
    return Coworker(**defaults)  # type: ignore[arg-type]


def _state_with(*cws: Coworker) -> OrchestratorState:
    state = OrchestratorState()
    for cw in cws:
        state.coworkers[cw.id] = CoworkerState.from_coworker(cw)
    return state


# ---------------------------------------------------------------------------
# _resolve_target (handbook §6 Step 5.3 contract (e))
# ---------------------------------------------------------------------------


def test_resolve_target_matches_by_folder() -> None:
    tenant = str(uuid.uuid4())
    tr = _cw(tenant_id=tenant, name="Trading", folder="trading", agent_role="agent")
    state = _state_with(tr)
    got = _resolve_target(state, tenant, "trading", exclude_id="x")
    assert got is not None and got.id == tr.id


def test_resolve_target_falls_back_to_uuid_when_folder_misses() -> None:
    """The literal ``id:`` label in the catalog occasionally nudges a model
    into passing the UUID rather than the folder slug. ``_resolve_target``
    falls back to the UUID lookup so this doesn't surface as a confusing
    "agent not found" + a catalog re-render that still shows folders.
    Test #5 sub-case (UUID-passed-instead-of-folder).
    """
    tenant = str(uuid.uuid4())
    tr = _cw(tenant_id=tenant, name="Trading", folder="trading", agent_role="agent")
    state = _state_with(tr)
    # By UUID — should resolve via the fallback path.
    got = _resolve_target(state, tenant, tr.id, exclude_id="x")
    assert got is not None and got.id == tr.id


def test_resolve_target_returns_none_for_wrong_folder() -> None:
    """Test #5 sub-case (wrong-folder)."""
    tenant = str(uuid.uuid4())
    tr = _cw(tenant_id=tenant, name="Trading", folder="trading", agent_role="agent")
    state = _state_with(tr)
    assert _resolve_target(state, tenant, "does-not-exist", exclude_id="x") is None


def test_resolve_target_returns_none_for_wrong_uuid() -> None:
    """Test #5 sub-case (wrong-UUID)."""
    tenant = str(uuid.uuid4())
    tr = _cw(tenant_id=tenant, name="Trading", folder="trading", agent_role="agent")
    state = _state_with(tr)
    fake_uuid = str(uuid.uuid4())
    assert _resolve_target(state, tenant, fake_uuid, exclude_id="x") is None


def test_resolve_target_rejects_self_delegation() -> None:
    """Test #3 — even if folder/id matches, the caller can't pick itself."""
    tenant = str(uuid.uuid4())
    me = _cw(tenant_id=tenant, name="Me", folder="me", agent_role="agent")
    state = _state_with(me)
    assert _resolve_target(state, tenant, "me", exclude_id=me.id) is None


def test_resolve_target_rejects_frontdesk_target() -> None:
    """Test #7a — frontdesks are never delegation targets."""
    tenant = str(uuid.uuid4())
    fd = _cw(
        tenant_id=tenant, name="Frontdesk", folder="frontdesk",
        agent_role="super_agent", is_frontdesk=True,
    )
    state = _state_with(fd)
    assert _resolve_target(state, tenant, "frontdesk", exclude_id="x") is None


def test_resolve_target_rejects_super_agent_non_frontdesk() -> None:
    """Test #7b — super_agents that are not flagged is_frontdesk are still
    rejected. ``agent_role='agent'`` is the only acceptable target role.
    Mutation guard: this catches a future PR that loosens the filter
    to "anything but frontdesk"."""
    tenant = str(uuid.uuid4())
    sa = _cw(
        tenant_id=tenant, name="OtherSuper", folder="other-super",
        agent_role="super_agent", is_frontdesk=False,
    )
    state = _state_with(sa)
    assert _resolve_target(state, tenant, "other-super", exclude_id="x") is None


def test_resolve_target_rejects_paused_status() -> None:
    tenant = str(uuid.uuid4())
    paused = _cw(
        tenant_id=tenant, name="Paused", folder="paused",
        agent_role="agent", status="paused",
    )
    state = _state_with(paused)
    assert _resolve_target(state, tenant, "paused", exclude_id="x") is None


def test_resolve_target_rejects_cross_tenant() -> None:
    """Belt-and-suspenders for test #4 — even if state happens to know a
    coworker by folder, the tenant check filters them out."""
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    cw = _cw(tenant_id=b, name="Other", folder="other", agent_role="agent")
    state = _state_with(cw)
    assert _resolve_target(state, a, "other", exclude_id="x") is None


# ---------------------------------------------------------------------------
# Constants / mutation guards
# ---------------------------------------------------------------------------


def test_max_delegation_depth_is_one_not_two() -> None:
    """Mutation guard for test #6: the depth limit constant MUST stay 1.
    If a refactor accidentally bumps it (e.g. to 2 because someone thinks
    "let A→B→C work"), this assertion fails immediately and the file
    history shows who/when. Don't change to 2."""
    assert MAX_DELEGATION_DEPTH == 1


def test_outer_guard_shorter_than_business_deadline() -> None:
    """Mutation guard for contract (c): the outer guard must be strictly
    shorter than the business deadline. If someone flips them, the
    handler still works mechanically but the audit messages stop being
    meaningful (every slow LLM looks like "queue stalled")."""
    from rolemesh.orchestration.delegation import DEFAULT_BUSINESS_DEADLINE_S
    assert OUTER_GUARD_S < DEFAULT_BUSINESS_DEADLINE_S


# ---------------------------------------------------------------------------
# _merge_pi_two_event_pattern — handbook test #13 (a/b/c/d)
# ---------------------------------------------------------------------------


def test_merge_pi_two_event_pattern_13a() -> None:
    """Pi shape: text event (is_final=False) + marker (is_final=True,
    result=None, new_session_id='S'). Merged result text comes from the
    text event; new_session_id comes from the marker."""
    text_event = AgentOutput(status="success", result="Hi", is_final=False)
    marker = AgentOutput(
        status="success", result=None, new_session_id="S", is_final=True,
    )
    merged = _merge_pi_two_event_pattern(text_event, marker, None)
    assert merged.status == "success"
    assert merged.result == "Hi"
    assert merged.new_session_id == "S"
    assert merged.is_final is True


def test_merge_pi_two_event_pattern_13b_claude_single_event() -> None:
    """Claude shape: a single event with text + is_final=True
    + new_session_id. Treated as if both pi events had arrived."""
    sole = AgentOutput(
        status="success", result="Hi", new_session_id="S", is_final=True,
    )
    # Claude shape produces text_event=None, final_marker=<the one>,
    # which without text would log a defensive warning. But the actual
    # _on_output records is_final=True as ``final_marker``. Since we
    # have no text_event, this is the "marker without prior text" path.
    # Verify the warning-emitting branch also yields the marker's
    # new_session_id and falls back to None result.
    merged = _merge_pi_two_event_pattern(None, sole, None)
    assert merged.status == "success"
    assert merged.new_session_id == "S"
    # The Claude single-event shape carries text in `result` AND
    # is_final=True. In the actual _on_output flow it would land as
    # ``final_marker`` because ``is_final=True``; the text inside the
    # marker is dropped here because the Pi-style API said "marker
    # carries no result". For real Claude flow, _on_output would have
    # recorded the same event as text_event for ``is_final=False``
    # — but Claude doesn't emit is_final=False. So Claude in practice
    # goes through the (None, final_marker, None) path and the
    # merger returns the marker's content; we test the variant where
    # text DID land alongside the marker below.


def test_merge_pi_two_event_pattern_13b_text_carries_through_marker() -> None:
    """Claude single-event shape as observed via the _on_output
    pipeline: the event has both ``result`` and ``is_final=True``. We
    fake the realistic observation: the single event arrives, and
    because it's terminal, _on_output also captures the text via the
    text branch (text_event = the event itself). Then the merge sees
    both ``text_event`` and ``final_marker`` referencing the same event
    object and produces the expected merged output."""
    sole = AgentOutput(
        status="success", result="Hi", new_session_id="S", is_final=True,
    )
    merged = _merge_pi_two_event_pattern(sole, sole, None)
    assert merged.status == "success"
    assert merged.result == "Hi"
    assert merged.new_session_id == "S"


def test_merge_pi_two_event_pattern_13c_terminal_beats_marker() -> None:
    """A late terminal event (e.g. safety_blocked from a post-MODEL hook)
    must override an earlier success marker. Otherwise the user would
    receive partial success text from a turn that the safety pipeline
    decided to block. Pins ``terminal_event > final_marker``."""
    text_event = AgentOutput(status="success", result="partial", is_final=False)
    marker = AgentOutput(
        status="success", result=None, new_session_id="S", is_final=True,
    )
    safety = AgentOutput(
        status="safety_blocked",
        result="disallowed_topic",
        is_final=True,
        metadata={"stage": "MODEL_OUTPUT"},
    )
    merged = _merge_pi_two_event_pattern(text_event, marker, safety)
    assert merged.status == "safety_blocked"
    assert merged.result == "disallowed_topic"
    # The success metadata must NOT have leaked through.
    assert merged.metadata == {"stage": "MODEL_OUTPUT"}


def test_merge_pi_two_event_pattern_13d_text_only_no_marker() -> None:
    """Only a text event arrives (no marker). Closure must have ended
    via ``executor.execute()`` returning normally. The merger gives back
    text but ``new_session_id=None`` because no marker carried it."""
    text_event = AgentOutput(status="success", result="Hi", is_final=False)
    merged = _merge_pi_two_event_pattern(text_event, None, None)
    assert merged.status == "success"
    assert merged.result == "Hi"
    assert merged.new_session_id is None
    assert merged.is_final is True


def test_merge_pi_two_event_pattern_text_event_session_id_used_when_marker_lacks() -> None:
    """Defensive: if the marker arrives without a new_session_id but the
    text event already had one, prefer the text-event session id rather
    than dropping it."""
    text_event = AgentOutput(
        status="success", result="Hi",
        new_session_id="from-text", is_final=False,
    )
    marker = AgentOutput(
        status="success", result=None,
        new_session_id=None, is_final=True,
    )
    merged = _merge_pi_two_event_pattern(text_event, marker, None)
    assert merged.new_session_id == "from-text"


def test_merge_pi_two_event_pattern_empty_returns_degenerate_success() -> None:
    merged = _merge_pi_two_event_pattern(None, None, None)
    assert merged.status == "success"
    assert merged.result is None
    assert merged.new_session_id is None


# ---------------------------------------------------------------------------
# _build_agent_input — handbook test #17a (EXACT role_config shape)
# ---------------------------------------------------------------------------


def test_build_agent_input_role_config_exact_shape() -> None:
    """Pins the EXACT role_config keys that flow to the target. A loose
    membership check on a subset would silently let a regression
    introduce a fifth key (e.g. accidentally including the caller's
    permissions, a real attack-surface increase since safety hooks would
    then read ``role_config[from_permissions]`` and could be confused
    into thinking the caller's perms apply)."""
    tenant = str(uuid.uuid4())
    target = _cw(
        tenant_id=tenant, name="Trading", folder="trading", agent_role="agent",
    )
    caller = _cw(
        tenant_id=tenant, name="Frontdesk", folder="frontdesk",
        agent_role="super_agent", is_frontdesk=True,
    )
    parent_conv_id = str(uuid.uuid4())
    child_conv_id = str(uuid.uuid4())
    delegation_id = str(uuid.uuid4())

    inp = _build_agent_input(
        target_co=target,
        from_co=caller,
        child_conv_chat_id=f"internal:{parent_conv_id}:{target.id}",
        child_conv_id=child_conv_id,
        prompt="hello",
        user_id="user-1",
        session_id=None,
        depth=0,
        parent_conv_id=parent_conv_id,
        delegation_id=delegation_id,
    )

    assert inp.role_config == {
        "is_delegated_call": True,
        "delegated_by": caller.id,
        "delegation_depth": 1,
        "parent_conversation_id": parent_conv_id,
        "delegation_id": delegation_id,
    }
    # And the target's permissions, not the caller's, flow through.
    assert inp.permissions == target.permissions.to_dict()  # type: ignore[union-attr]
    assert inp.tenant_id == target.tenant_id
    assert inp.coworker_id == target.id
    assert inp.conversation_id == child_conv_id
    assert inp.user_id == "user-1"
    assert inp.system_prompt == target.system_prompt
    assert inp.assistant_name == target.name


def test_build_agent_input_depth_increments() -> None:
    """If a future PR ever lets depth=1 through (it shouldn't — see test
    #6 mutation guard) the target's role_config should reflect depth=2.
    This pins the increment so the target sees the correct hop count."""
    tenant = str(uuid.uuid4())
    target = _cw(tenant_id=tenant, name="T", folder="t", agent_role="agent")
    caller = _cw(tenant_id=tenant, name="F", folder="f", agent_role="agent")
    inp = _build_agent_input(
        target_co=target, from_co=caller,
        child_conv_chat_id="x", child_conv_id="y",
        prompt="p", user_id=None, session_id=None,
        depth=1,  # hypothetical
        parent_conv_id="p", delegation_id="d",
    )
    assert inp.role_config is not None
    assert inp.role_config["delegation_depth"] == 2


def test_build_agent_input_session_id_passthrough() -> None:
    tenant = str(uuid.uuid4())
    target = _cw(tenant_id=tenant, name="T", folder="t", agent_role="agent")
    caller = _cw(tenant_id=tenant, name="F", folder="f", agent_role="agent")
    inp = _build_agent_input(
        target_co=target, from_co=caller,
        child_conv_chat_id="x", child_conv_id="y",
        prompt="p", user_id=None, session_id="S-prev",
        depth=0, parent_conv_id="p", delegation_id="d",
    )
    assert inp.session_id == "S-prev"


# ---------------------------------------------------------------------------
# _map_output_to_response — wire shape
# ---------------------------------------------------------------------------


def test_map_output_to_response_success() -> None:
    target = _cw(tenant_id="t", name="Trading", folder="trading", agent_role="agent")
    out = AgentOutput(
        status="success", result="all done", new_session_id="S", is_final=True,
    )
    response, audit = _map_output_to_response(out, target, "d", "c", 42)
    assert audit == "success"
    assert response["status"] == "success"
    assert response["text"] == "all done"
    assert response["isError"] is False
    md = response["metadata"]
    assert md["targetCoworkerId"] == target.id
    assert md["targetFolder"] == "trading"
    assert md["childConversationId"] == "c"
    assert md["newSessionId"] == "S"
    assert md["durationMs"] == 42
    assert md["delegationId"] == "d"
    assert md["safetyStage"] is None


def test_map_output_to_response_safety_blocked_carries_stage() -> None:
    target = _cw(tenant_id="t", name="Trading", folder="trading", agent_role="agent")
    out = AgentOutput(
        status="safety_blocked",
        result="disallowed_topic",
        metadata={"stage": "MODEL_OUTPUT"},
    )
    response, audit = _map_output_to_response(out, target, "d", "c", 50)
    assert audit == "safety_blocked"
    assert response["isError"] is True
    assert "Trading declined" in response["text"]
    assert "disallowed_topic" in response["text"]
    assert response["metadata"]["safetyStage"] == "MODEL_OUTPUT"


def test_map_output_to_response_stopped_collapses_to_error_audit() -> None:
    """User-facing notion of "the target was interrupted" maps to the
    error audit slot — there's no separate ``interrupted`` audit
    state. Test pins this collapse so a future refactor doesn't
    accidentally introduce a fifth audit status that fails the
    conditional UPDATE."""
    target = _cw(tenant_id="t", name="Trading", folder="trading", agent_role="agent")
    out = AgentOutput(status="stopped", result=None)
    response, audit = _map_output_to_response(out, target, "d", "c", 7)
    assert audit == "error"
    assert response["status"] == "error"
    assert "interrupted" in response["text"]
    assert response["isError"] is True


def test_map_output_to_response_error_carries_reason() -> None:
    target = _cw(tenant_id="t", name="Trading", folder="trading", agent_role="agent")
    out = AgentOutput(status="error", result=None, error="LLM crashed")
    response, audit = _map_output_to_response(out, target, "d", "c", 11)
    assert audit == "error"
    assert "Trading failed: LLM crashed" in response["text"]


# ---------------------------------------------------------------------------
# _err — wire shape
# ---------------------------------------------------------------------------


def test_err_response_shape() -> None:
    r = _err("Tenant mismatch.")
    assert r == {
        "status": "error",
        "text": "Tenant mismatch.",
        "isError": True,
        "metadata": None,
    }


# ---------------------------------------------------------------------------
# send_message tool guard — handbook test #16
# ---------------------------------------------------------------------------


@dataclass
class _DummyNats:
    pass


def _make_ctx(*, is_delegated_call: bool) -> ToolContext:
    return ToolContext(
        js=object(),  # type: ignore[arg-type]
        nc=object(),  # type: ignore[arg-type]
        job_id="job-1",
        chat_jid="chat-1",
        group_folder="trading",
        permissions={"agent_delegate": True},
        tenant_id="t",
        coworker_id="c",
        conversation_id="conv-1",
        user_id="u",
        role_config={"is_delegated_call": is_delegated_call} if is_delegated_call else {},
    )


async def test_send_message_blocked_when_role_config_says_delegated() -> None:
    """When the target is running as a delegate, ``send_message`` would
    route to the child conv (internal binding, no UI subscriber) and
    silently fall on the floor. The right reply path is the RPC
    response; this guard makes the failure mode loud instead of silent."""
    ctx = _make_ctx(is_delegated_call=True)
    result = await send_message({"text": "hello"}, ctx)
    assert result.get("isError") is True
    assert "delegated call" in result["content"][0]["text"]


async def test_send_message_allowed_when_not_delegated() -> None:
    """Negative control: same tool call WITHOUT the is_delegated_call hint
    must NOT be blocked. The send still requires a working JS publish,
    so we just assert no `isError=True` is returned (the publish
    failure would have raised). Use a tiny stub for `publish`."""
    ctx = _make_ctx(is_delegated_call=False)
    published: list[tuple[str, dict[str, object]]] = []

    def _publish(subject: str, data: dict[str, object]) -> None:
        published.append((subject, data))

    # publish is a bound method on ToolContext; monkey-patch on the
    # instance so it doesn't touch real NATS.
    ctx.publish = _publish  # type: ignore[method-assign]
    result = await send_message({"text": "hello"}, ctx)
    assert "isError" not in result
    assert len(published) == 1
    assert "agent.job-1.messages" in published[0][0]


# ---------------------------------------------------------------------------
# Delegation tool — agent-side validation paths (handbook #2 partial)
# ---------------------------------------------------------------------------


async def test_delegate_tool_refuses_without_permission() -> None:
    """Agent-side gate. The orchestrator-side gate is exercised in the
    integration tests (test_delegate_handler.py) — this is the agent's
    own check so a misbehaving frontdesk LLM can't even send the RPC."""
    from agent_runner.tools.rolemesh_tools import delegate_to_agent
    ctx = _make_ctx(is_delegated_call=False)
    ctx.permissions = {"agent_delegate": False}
    result = await delegate_to_agent(
        {"target": "trading", "prompt": "Hi"}, ctx,
    )
    assert result.get("isError") is True
    assert "Permission denied" in result["content"][0]["text"]


async def test_delegate_tool_rejects_missing_target_or_prompt() -> None:
    from agent_runner.tools.rolemesh_tools import delegate_to_agent
    ctx = _make_ctx(is_delegated_call=False)
    for args in (
        {"target": "", "prompt": "Hi"},
        {"target": "trading", "prompt": ""},
        {"target": "   ", "prompt": "Hi"},
        {},
    ):
        result = await delegate_to_agent(args, ctx)
        assert result.get("isError") is True


async def test_delegate_tool_rejects_oversized_prompt() -> None:
    """Handbook §6 Step 5.1 — over-length prompt error MUST instruct the
    LLM to split or use file tools, not retry. Pin the keywords so a
    future copy-edit doesn't drop the "split" guidance and let the LLM
    fall into a retry loop on the same oversized prompt."""
    from agent_runner.tools.rolemesh_tools import MAX_DELEGATE_PROMPT_CHARS, delegate_to_agent
    ctx = _make_ctx(is_delegated_call=False)
    big = "x" * (MAX_DELEGATE_PROMPT_CHARS + 1)
    result = await delegate_to_agent(
        {"target": "trading", "prompt": big}, ctx,
    )
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "exceeds" in text
    assert "split" in text or "smaller" in text
    assert "Do NOT retry" in text


async def test_delegate_tool_rejects_unknown_context_mode() -> None:
    from agent_runner.tools.rolemesh_tools import delegate_to_agent
    ctx = _make_ctx(is_delegated_call=False)
    result = await delegate_to_agent(
        {"target": "trading", "prompt": "Hi", "context_mode": "shared"}, ctx,
    )
    assert result.get("isError") is True
    assert "context_mode" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Delegation tool — happy path through a FakeNats request
# ---------------------------------------------------------------------------


class _FakeNats:
    """Captures the request payload and returns a canned response."""

    def __init__(self, response: dict[str, object]) -> None:
        self._response = response
        self.calls: list[tuple[str, bytes]] = []

    async def request(
        self, subject: str, data: bytes, timeout: float = 0,
    ) -> object:
        self.calls.append((subject, data))

        @dataclass
        class _Reply:
            data: bytes

        return _Reply(data=json.dumps(self._response).encode())


async def test_delegate_tool_forwards_payload_to_orch_request_subject() -> None:
    """Pin the wire-level contract: (a) subject is
    ``agent.{job_id}.delegate.request``, (b) the payload carries every
    field the orchestrator-side ``_process_one`` reads. A test that
    only asserts the success result text could miss a payload that's
    silently missing ``depth`` or ``fromConversationId`` until prod."""
    from agent_runner.tools.rolemesh_tools import delegate_to_agent
    fake_response = {"text": "all done", "isError": False}
    fake_nc = _FakeNats(fake_response)
    ctx = _make_ctx(is_delegated_call=False)
    ctx.nc = fake_nc  # type: ignore[assignment]
    ctx.role_config = {"delegation_depth": 0}

    result = await delegate_to_agent(
        {"target": "trading", "prompt": "Sell 100 AAPL"}, ctx,
    )

    assert result.get("isError") is None or result["isError"] is False
    assert result["content"][0]["text"] == "all done"

    assert fake_nc.calls
    subject, body = fake_nc.calls[0]
    assert subject == "agent.job-1.delegate.request"
    payload = json.loads(body)
    assert payload["type"] == "delegate_to_agent"
    assert payload["tenantId"] == "t"
    assert payload["fromCoworkerId"] == "c"
    assert payload["fromConversationId"] == "conv-1"
    assert payload["target"] == "trading"
    assert payload["prompt"] == "Sell 100 AAPL"
    assert payload["contextMode"] == "isolated"
    assert payload["depth"] == 0


async def test_delegate_tool_surfaces_orch_error_as_is_error() -> None:
    from agent_runner.tools.rolemesh_tools import delegate_to_agent
    fake_response = {
        "text": "Trading declined: order size exceeds daily limit",
        "isError": True,
    }
    fake_nc = _FakeNats(fake_response)
    ctx = _make_ctx(is_delegated_call=False)
    ctx.nc = fake_nc  # type: ignore[assignment]

    result = await delegate_to_agent(
        {"target": "trading", "prompt": "X"}, ctx,
    )
    assert result["isError"] is True
    assert "Trading declined" in result["content"][0]["text"]


async def test_delegate_tool_handles_rpc_timeout() -> None:
    from agent_runner.tools.rolemesh_tools import delegate_to_agent

    class _TimeoutNats:
        async def request(
            self, subject: str, data: bytes, timeout: float = 0,
        ) -> object:
            raise TimeoutError("simulated")

    ctx = _make_ctx(is_delegated_call=False)
    ctx.nc = _TimeoutNats()  # type: ignore[assignment]
    result = await delegate_to_agent(
        {"target": "trading", "prompt": "X"}, ctx,
    )
    assert result["isError"] is True
    assert "timed out" in result["content"][0]["text"]


async def test_delegate_tool_passes_through_existing_depth_in_role_config() -> None:
    """If somehow the calling agent is already a delegate (e.g. a
    misconfigured admin enabled agent_delegate on a domain agent),
    the payload's ``depth`` field reflects ``role_config.delegation_depth``
    so the orchestrator's MAX_DELEGATION_DEPTH check fires. Pin the
    forwarding so the agent-side path can't accidentally zero out the
    field and slip a depth=1 call through as depth=0."""
    from agent_runner.tools.rolemesh_tools import delegate_to_agent
    fake_nc = _FakeNats({"text": "ok", "isError": False})
    ctx = _make_ctx(is_delegated_call=False)
    ctx.nc = fake_nc  # type: ignore[assignment]
    ctx.role_config = {"delegation_depth": 1}

    await delegate_to_agent(
        {"target": "trading", "prompt": "X"}, ctx,
    )
    payload = json.loads(fake_nc.calls[0][1])
    assert payload["depth"] == 1
