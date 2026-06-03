"""``agent.*.messages`` IPC delivery contract.

Two regimes share this handler:

- Interactive turns (history): the original duplicate-delivery bug
  fixed by commit a67d3e6. The handler still log-and-drops those —
  ``agent.*.results`` is the source of truth for user-visible replies,
  and ``send_message`` tool IPCs would just double-send. The drop
  contract has its own regression guards (no ``_ipc_sent_texts``,
  ``_send_via_coworker`` never reached on this branch).

- Scheduled-task turns (today): ``_run_task``'s ``_on_output`` only
  forwards when the agent produces a final ``result``. Agents that
  call ``send_message`` for "remind me at T" prompts typically
  produce no separate result, leaving the natural-output path empty
  — the IPC IS the only delivery. The tool stamps
  ``isScheduledTask=True`` so this handler routes them into
  ``_send_via_coworker`` instead of dropping.
"""

from __future__ import annotations

from rolemesh.main import _handle_agent_message_ipc

# ---------------------------------------------------------------------------
# Interactive-turn drop (a67d3e6 contract)
# ---------------------------------------------------------------------------


async def test_interactive_payload_does_not_invoke_send_via_coworker() -> None:
    """A well-formed send_message IPC WITHOUT ``isScheduledTask`` is an
    interactive turn. The natural-output path delivers the reply;
    forwarding here would double-send. We monkey-patch
    ``_send_via_coworker`` to raise so any accidental forward fails
    visibly — this is the original a67d3e6 contract.
    """
    import rolemesh.main as m

    original = m._send_via_coworker
    called: list[tuple] = []

    async def _boom(*args, **kwargs) -> None:
        called.append((args, kwargs))
        raise AssertionError(
            "_send_via_coworker must NOT be called for interactive "
            "send_message IPC — that's the duplicate-delivery bug."
        )

    m._send_via_coworker = _boom  # type: ignore[assignment]
    try:
        await _handle_agent_message_ipc({
            "type": "message",
            "chatJid": "chat-abc",
            "text": "Hello from agent tool",
            "groupFolder": "tenant-1/coworker-adam",
            "tenantId": "t-1",
            "coworkerId": "cw-1",
            # No isScheduledTask key → defaults to False
        })
    finally:
        m._send_via_coworker = original  # type: ignore[assignment]
    assert called == []


async def test_interactive_payload_with_explicit_false_also_drops() -> None:
    """Explicit ``isScheduledTask=False`` (the default that the tool
    actually stamps post-fix) takes the same drop branch. Pinned
    separately from the absent-key case so a future schema change
    that flips defaults can't silently flip behaviour."""
    import rolemesh.main as m

    original = m._send_via_coworker
    called: list[tuple] = []

    async def _boom(*_args, **_kwargs) -> None:
        called.append(())
        raise AssertionError("forward fired on interactive payload")

    m._send_via_coworker = _boom  # type: ignore[assignment]
    try:
        await _handle_agent_message_ipc({
            "type": "message",
            "chatJid": "chat-abc",
            "text": "hi",
            "isScheduledTask": False,
            "coworkerId": "cw-1",
            "groupFolder": "tenant-1/coworker-adam",
        })
    finally:
        m._send_via_coworker = original  # type: ignore[assignment]
    assert called == []


# ---------------------------------------------------------------------------
# Scheduled-task forward (the new path)
# ---------------------------------------------------------------------------


async def test_scheduled_task_payload_forwards_to_send_via_coworker() -> None:
    """The new forward branch: ``isScheduledTask=True`` + a resolvable
    coworker → exactly one ``_send_via_coworker`` call with the same
    chat_jid + text. Without this branch, scheduled-task replies
    silently vanish (the original symptom that motivated the fix).
    """
    import rolemesh.main as m
    from rolemesh.core.orchestrator_state import (
        CoworkerState,
        OrchestratorState,
    )

    # Minimal in-memory state: one tenant, one coworker keyed by id.
    state = OrchestratorState()
    cw_cfg = _make_coworker_stub(coworker_id="cw-sched", tenant_id="t-sched")
    state.coworkers["cw-sched"] = CoworkerState.from_coworker(cw_cfg)
    original_state = m._state
    original_send = m._send_via_coworker
    captured: list[tuple[object, str, str]] = []

    async def _capture(cw_state, chat_id: str, text: str) -> None:
        captured.append((cw_state, chat_id, text))

    m._state = state  # type: ignore[assignment]
    m._send_via_coworker = _capture  # type: ignore[assignment]
    try:
        await _handle_agent_message_ipc({
            "type": "message",
            "chatJid": "8326882447",
            "text": "Reminder: 2 minutes are up!",
            "isScheduledTask": True,
            "coworkerId": "cw-sched",
            "groupFolder": "tenant-1/adam",
        })
    finally:
        m._state = original_state  # type: ignore[assignment]
        m._send_via_coworker = original_send  # type: ignore[assignment]

    assert len(captured) == 1
    fwd_cw_state, fwd_chat_id, fwd_text = captured[0]
    assert fwd_chat_id == "8326882447"
    assert fwd_text == "Reminder: 2 minutes are up!"
    assert fwd_cw_state is state.coworkers["cw-sched"]


async def test_scheduled_task_payload_falls_back_to_group_folder_lookup() -> None:
    """If ``coworkerId`` is missing, the resolver falls back to
    ``groupFolder`` (the coworker's ``folder`` field). Pinned so the
    fallback path stays alive — the orchestrator's tasks-IPC handler
    uses the same pattern for trust-boundary reasons (claimed
    coworker_id from the payload is not authoritative; folder→state
    lookup is). A regression here would silently drop scheduled-task
    deliveries whenever the agent runner omits the field.
    """
    import rolemesh.main as m
    from rolemesh.core.orchestrator_state import (
        CoworkerState,
        OrchestratorState,
    )
    from rolemesh.core.types import Tenant

    state = OrchestratorState()
    state.tenants["t-1"] = Tenant(
        id="t-1", slug="t-1", name="T1", plan=None,
        max_concurrent_containers=5,
    )
    cw_cfg = _make_coworker_stub(
        coworker_id="cw-by-folder", tenant_id="t-1", folder="folder-adam",
    )
    state.coworkers["cw-by-folder"] = CoworkerState.from_coworker(cw_cfg)

    original_state = m._state
    original_send = m._send_via_coworker
    captured: list[tuple] = []

    async def _capture(cw_state, chat_id: str, text: str) -> None:
        captured.append((cw_state, chat_id, text))

    m._state = state  # type: ignore[assignment]
    m._send_via_coworker = _capture  # type: ignore[assignment]
    try:
        await _handle_agent_message_ipc({
            "type": "message",
            "chatJid": "chat-1",
            "text": "via folder",
            "isScheduledTask": True,
            # coworkerId deliberately omitted
            "groupFolder": "folder-adam",
        })
    finally:
        m._state = original_state  # type: ignore[assignment]
        m._send_via_coworker = original_send  # type: ignore[assignment]

    assert len(captured) == 1
    assert captured[0][1] == "chat-1"
    assert captured[0][2] == "via folder"


async def test_scheduled_task_with_unresolvable_coworker_does_not_forward() -> None:
    """Defensive: if neither ``coworkerId`` nor ``groupFolder`` resolves
    against ``_state`` (an orphan task that fires after its coworker
    was deleted, or a race during shutdown), we MUST NOT call
    ``_send_via_coworker`` with ``None`` — that would crash inside
    the gateway lookup. Drop with a warning instead.
    """
    import rolemesh.main as m
    from rolemesh.core.orchestrator_state import OrchestratorState

    state = OrchestratorState()  # no coworkers
    original_state = m._state
    original_send = m._send_via_coworker
    called: list[tuple] = []

    async def _capture(*args, **kwargs) -> None:
        called.append((args, kwargs))

    m._state = state  # type: ignore[assignment]
    m._send_via_coworker = _capture  # type: ignore[assignment]
    try:
        await _handle_agent_message_ipc({
            "type": "message",
            "chatJid": "chat-x",
            "text": "ghost",
            "isScheduledTask": True,
            "coworkerId": "does-not-exist",
            "groupFolder": "also-not-found",
        })
    finally:
        m._state = original_state  # type: ignore[assignment]
        m._send_via_coworker = original_send  # type: ignore[assignment]

    assert called == []


# ---------------------------------------------------------------------------
# Malformed payload handling (unchanged from a67d3e6 contract)
# ---------------------------------------------------------------------------


async def test_missing_type_silently_skipped() -> None:
    await _handle_agent_message_ipc({"chatJid": "c", "text": "t"})


async def test_missing_chat_jid_silently_skipped() -> None:
    await _handle_agent_message_ipc({"type": "message", "text": "stray"})


async def test_empty_text_silently_skipped() -> None:
    await _handle_agent_message_ipc({
        "type": "message", "chatJid": "c", "text": "",
    })


# ---------------------------------------------------------------------------
# Module-global guards (a67d3e6 contract)
# ---------------------------------------------------------------------------


def test_ipc_sent_texts_module_global_was_removed() -> None:
    """Direct regression guard: the string-match dedup set must NOT
    come back. Any reintroduction would reintroduce the race that the
    whole refactor existed to kill."""
    import rolemesh.main as m
    assert not hasattr(m, "_ipc_sent_texts"), (
        "rolemesh.main._ipc_sent_texts must stay removed — "
        "race-prone dedup."
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_coworker_stub(
    *,
    coworker_id: str,
    tenant_id: str,
    folder: str = "stub-folder",
):
    """Build a minimal Coworker dataclass for in-memory state seeding."""
    from rolemesh.core.types import Coworker

    return Coworker(
        id=coworker_id,
        tenant_id=tenant_id,
        name="Stub",
        folder=folder,
        agent_backend="claude",
    )
