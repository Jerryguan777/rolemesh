"""Regression test for context pollution after Stop.

Scenario (from a reported real bug):
  - User asks Q1 (a substantive question).
  - Pi streams partial assistant reply, user clicks Stop mid-stream.
  - Abort fires; a SessionMessageEntry for Q1 + an aborted assistant entry
    are already on disk and chained as the active leaf.
  - User asks Q2 (unrelated, e.g. "who are you?").
  - Q2 was persisted as a child of the aborted assistant, so
    build_session_context() walks Q2 -> aborted_assistant -> Q1 -> ... and
    Pi's next LLM call sees the cancelled question again. Reply conflates
    both questions ("I'm an assistant... also, to group by country exposure
    I need you to choose 1 or 2").

The fix rewinds session_manager._leaf_id to the snapshot taken at the
start of the aborted prompt, AND rebuilds the agent's in-memory message
list from that rewound leaf. The aborted turn stays on disk as an orphan
branch (for debug), but the active parent chain no longer includes it.

These tests pin the session-manager half of the mechanism (rewind +
build_session_context returning a clean path). The agent.replace_messages
half is exercised by Pi's own `sdk.py:270` resume-session code path,
which uses the same API our abort fix invokes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pi.ai.types import AssistantMessage, TextContent, UserMessage
from pi.coding_agent.core.session_manager import SessionManager


def _assistant(text: str, stop_reason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        api="openai-responses",
        provider="openai",
        model="gpt-4o-mini",
        stop_reason=stop_reason,  # type: ignore[arg-type]
    )


def _user(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def _texts(messages: list) -> list[str]:
    """Extract text content per message for readable assertions."""
    out: list[str] = []
    for m in messages:
        content = getattr(m, "content", "")
        if isinstance(content, str):
            out.append(content)
        else:
            parts = [b.text for b in content if isinstance(b, TextContent)]
            out.append("".join(parts))
    return out


@pytest.fixture
def sm(tmp_path: Path) -> SessionManager:
    """Fresh SessionManager writing to a tmp file."""
    mgr = SessionManager.create(str(tmp_path))
    mgr.set_session_file(str(tmp_path / "session.jsonl"))
    return mgr


def test_context_excludes_aborted_branch_after_leaf_rewind(sm: SessionManager) -> None:
    """Core bug repro: without rewind, Q2's context includes Q1+aborted.
    After rewind to pre-Q1 leaf, context for Q2 contains only Q0 turn
    and Q2 itself."""

    # Q0 turn (completed)
    sm.append_message(_user("Q0 what is 2+2"))
    sm.append_message(_assistant("Q0 reply: 4"))

    # Snapshot leaf BEFORE Q1 — this is what AgentSession.prompt() captures.
    pre_q1_leaf = sm.get_leaf_id()

    # Q1 turn, user cancels mid-stream: partial aborted assistant appended
    sm.append_message(_user("Q1 pls show me 7Cap_T1_US by country exposure"))
    sm.append_message(_assistant("Q1 partial: I can help, which do you mean by country...", stop_reason="aborted"))

    # Sanity: without rewind the active chain would carry Q1 + aborted
    # forward to the next turn. Verify that first.
    sm.append_message(_user("Q2 who are you"))
    ctx_without_rewind = sm.build_session_context()
    texts_no_rewind = _texts(ctx_without_rewind.messages)
    assert any("Q1" in t for t in texts_no_rewind), (
        "sanity check failed: without rewind, Q1 should still appear in the chain"
    )

    # Now simulate what abort() does:
    #   1. Remove Q2 we just appended (it was appended on top of the dirty chain)
    #   2. Rewind to pre-Q1 leaf
    #   3. Re-append Q2 (same text) — this is what the next prompt() would do
    # Step 1 is only needed in this test because we already appended Q2 above
    # for the sanity check; in real flow Q2 is appended after rewind.
    sm._leaf_id = pre_q1_leaf
    sm.append_message(_user("Q2 who are you"))

    ctx = sm.build_session_context()
    texts = _texts(ctx.messages)

    # Active chain after rewind: Q0_user, Q0_reply, Q2. No Q1.
    assert any("Q0 what is 2+2" in t for t in texts)
    assert any("Q0 reply: 4" in t for t in texts)
    assert any("Q2 who are you" in t for t in texts)
    # The regression assertion — this is the line that catches the original bug.
    assert not any("Q1" in t for t in texts), (
        f"aborted turn leaked into Q2's context: {texts}"
    )
    assert not any("partial" in t for t in texts), (
        f"aborted partial reply leaked into Q2's context: {texts}"
    )


def test_rewind_to_none_handles_empty_pre_prompt_state(sm: SessionManager) -> None:
    """When the user aborts their very first message (no prior turns), the
    pre-prompt leaf snapshot is None. reset_leaf() must produce a clean
    context for the next turn."""

    # Pre-prompt state: empty session, leaf is None
    assert sm.get_leaf_id() is None

    # User sends Q1 and aborts immediately
    sm.append_message(_user("Q1 first ever message"))
    sm.append_message(_assistant("aborted partial", stop_reason="aborted"))

    # Rewind: equivalent to reset_leaf() since pre-prompt leaf was None
    sm.reset_leaf()
    assert sm.get_leaf_id() is None

    # Q2 lands at the root
    sm.append_message(_user("Q2 second message"))

    ctx = sm.build_session_context()
    texts = _texts(ctx.messages)
    assert texts == ["Q2 second message"], (
        f"expected only Q2, got {texts}"
    )


def test_orphaned_branch_remains_in_file_entries(sm: SessionManager) -> None:
    """Aborted entries should stay in the linear file for debug/audit, only
    excluded from the parent-chain walk. Regression guards against a future
    'fix' that deletes entries outright."""

    sm.append_message(_user("Q0"))
    sm.append_message(_assistant("Q0 reply"))
    pre = sm.get_leaf_id()

    sm.append_message(_user("Q1 to be orphaned"))
    sm.append_message(_assistant("Q1 partial", stop_reason="aborted"))

    sm._leaf_id = pre
    sm.append_message(_user("Q2"))

    # build_session_context (active chain walk): Q1 absent
    texts_active = _texts(sm.build_session_context().messages)
    assert not any("orphaned" in t for t in texts_active)

    # But get_entries (flat file) still has all 5 messages
    all_message_texts = _texts(
        [getattr(e, "message", None) for e in sm.get_entries() if hasattr(e, "message")]
    )
    assert any("Q1 to be orphaned" in t for t in all_message_texts), (
        "orphaned entry was removed from the file — should have stayed for audit"
    )
    assert any("Q1 partial" in t for t in all_message_texts)
