"""Regression: agent.*.messages IPC no longer duplicates user-visible replies.

Historical background:
  rolemesh/main.py used to subscribe to ``agent.*.messages`` (published
  by the agent-side ``send_message`` tool) AND ``agent.*.results``
  (natural LLM output) and forward BOTH to the channel gateway. Claude
  Code's model routinely echoes its tool-called text in the final
  ResultMessage, so the same text reached the user twice. A string-
  match dedup set (``_ipc_sent_texts``) papered over the common case
  but was race-prone: the two NATS subscriptions are independent
  consumers with no delivery-order guarantee, and under load ``.results``
  could arrive before ``.messages`` could register the text.

This PR removed the duplicate-delivery path entirely: the IPC handler
now only logs and drops. These tests pin that contract so a future
refactor cannot reintroduce the duplication.
"""

from __future__ import annotations

import pytest

from rolemesh.main import _handle_agent_message_ipc

# The orchestrator uses structlog rendered to stderr directly (see
# rolemesh.core.logger), which bypasses Python's logging module and
# therefore bypasses pytest's caplog. Use capsys to capture stderr.


def test_valid_message_payload_returns_without_raising() -> None:
    """A well-formed send_message IPC is accepted and dropped without
    error. The crucial guarantee is that the handler does NOT call
    _send_via_coworker — there's no second delivery path. The handler
    is pure log-and-drop: from this test's perspective the best we
    can observe is that no exception is raised and no side effect
    bubbles up. Together with
    test_ipc_sent_texts_module_global_was_removed this pins the
    removal of the dual-delivery codepath."""
    # No assertion on return value — function is void. Any exception
    # would fail the test, which is the contract.
    _handle_agent_message_ipc({
        "type": "message",
        "chatJid": "chat-abc",
        "text": "Hello from agent tool",
        "groupFolder": "tenant-1/coworker-adam",
        "tenantId": "t-1",
        "coworkerId": "cw-1",
    })


def test_handler_does_not_import_send_via_coworker() -> None:
    """Contract: the message IPC handler must not reach ``_send_via_coworker``.
    If a future change makes it forward messages again (reintroducing
    the duplicate-delivery bug), this test fails because
    ``_send_via_coworker`` would be invoked. We monkey-patch it to
    raise so any accidental call short-circuits visibly."""
    import rolemesh.main as m

    original = m._send_via_coworker
    called: list[tuple] = []

    async def _boom(*args, **kwargs) -> None:
        called.append((args, kwargs))
        raise AssertionError(
            "_send_via_coworker must NOT be called from _handle_agent_message_ipc "
            "— the redundant-delivery path was removed to fix duplicate replies."
        )

    m._send_via_coworker = _boom  # type: ignore[assignment]
    try:
        _handle_agent_message_ipc({
            "type": "message",
            "chatJid": "chat-xyz",
            "text": "attempt",
        })
    finally:
        m._send_via_coworker = original  # type: ignore[assignment]

    assert called == [], "send_via_coworker was called — regression!"


def test_missing_type_silently_skipped() -> None:
    """Non-message payloads on this subject (e.g. future control frames)
    fall through with no log and no error. Skip — don't error — so a
    forward-compatible schema expansion doesn't break the handler."""
    _handle_agent_message_ipc({"chatJid": "c", "text": "t"})  # no "type"


def test_missing_chat_jid_silently_skipped() -> None:
    """Malformed payload (type=message but no chatJid) is skipped.
    Matches the previous orchestrator behavior — the pre-refactor
    handler also required both chatJid AND text to be truthy before
    acting, so we preserve the same input-validation contract."""
    _handle_agent_message_ipc({"type": "message", "text": "stray"})


def test_empty_text_silently_skipped() -> None:
    _handle_agent_message_ipc({
        "type": "message", "chatJid": "c", "text": ""
    })


def test_ipc_sent_texts_module_global_was_removed() -> None:
    """Direct regression guard: the string-match dedup set must NOT
    come back. Any reintroduction would reintroduce the race that the
    whole refactor existed to kill. If you need per-turn state for a
    future feature, use a local closure inside _process_conversation_messages
    rather than module-global mutable state."""
    import rolemesh.main as m
    assert not hasattr(m, "_ipc_sent_texts"), (
        "rolemesh.main._ipc_sent_texts must stay removed — "
        "it is a race-prone dedup that tried to cover the "
        "dual-delivery bug (see PR removing it)."
    )
