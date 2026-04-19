"""TranscriptArchiveHandler tests.

Covers both branches:
  - Claude: JSONL transcript file + optional sessions-index.json summary
  - Pi: list of in-memory AgentMessage objects

Edge cases deliberately exercised (these are the ones most likely to hurt
in production and least likely to show up in a mirror test):
  - empty / all-error transcript => no file written
  - content that is a string (some user messages) vs list (assistant)
  - invalid JSON lines in the middle of a transcript
  - summary with non-ASCII / special characters that must be sanitized
  - long messages truncated with ellipsis
  - missing sessions-index.json: graceful fallback to generated name
  - unknown role in the messages list: silently skipped, not crashed
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used at runtime via pytest tmp_path
from typing import Any

from agent_runner.hooks import CompactionEvent
from agent_runner.hooks.handlers.transcript_archive import (
    TranscriptArchiveHandler,
    _sanitize_filename,
)

# ---------------------------------------------------------------------------
# Helpers — construct Claude-style JSONL transcripts and Pi messages
# ---------------------------------------------------------------------------


def _claude_entry_user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"content": text}}


def _claude_entry_assistant(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _write_transcript(path: Path, entries: list[dict[str, Any]]) -> None:
    lines = [json.dumps(e) for e in entries]
    path.write_text("\n".join(lines))


@dataclass
class _FakePiBlock:
    text: str = ""


@dataclass
class _FakePiMessage:
    role: str = "user"
    content: Any = field(default_factory=list)


# ---------------------------------------------------------------------------
# Claude branch
# ---------------------------------------------------------------------------


async def test_claude_archive_happy_path(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            _claude_entry_user("what time is it?"),
            _claude_entry_assistant("It is 3pm."),
            _claude_entry_user("thanks"),
            _claude_entry_assistant("You're welcome."),
        ],
    )
    archive_dir = tmp_path / "archive"
    handler = TranscriptArchiveHandler(
        assistant_name="Friday", archive_dir=archive_dir
    )

    await handler.on_pre_compact(
        CompactionEvent(transcript_path=str(transcript), session_id="sess-1")
    )

    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    # Each role line appears; assistant is rendered with the configured name
    assert "**User**: what time is it?" in body
    assert "**Friday**: It is 3pm." in body
    assert "**User**: thanks" in body
    assert "**Friday**: You're welcome." in body


async def test_claude_archive_uses_session_summary_for_filename(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [_claude_entry_user("hi"), _claude_entry_assistant("hey")],
    )
    # sessions-index.json lives alongside the transcript
    (tmp_path / "sessions-index.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "sessionId": "sess-2",
                        "summary": "Debugging the foo bar (v2)!",
                    }
                ]
            }
        )
    )
    archive_dir = tmp_path / "archive"
    handler = TranscriptArchiveHandler(archive_dir=archive_dir)

    await handler.on_pre_compact(
        CompactionEvent(transcript_path=str(transcript), session_id="sess-2")
    )
    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    # Filename incorporates a sanitized slug derived from the summary
    assert "debugging-the-foo-bar-v2" in files[0].name
    # The first-line heading carries the raw summary
    assert files[0].read_text().startswith("# Debugging the foo bar (v2)!")


async def test_claude_archive_missing_index_falls_back(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, [_claude_entry_user("hi")])
    archive_dir = tmp_path / "archive"
    handler = TranscriptArchiveHandler(archive_dir=archive_dir)

    await handler.on_pre_compact(
        CompactionEvent(transcript_path=str(transcript), session_id="no-such-sess")
    )
    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    # Fallback filename format: conversation-HHMM
    assert "conversation-" in files[0].name
    # Default heading when no summary is present
    assert files[0].read_text().startswith("# Conversation")


async def test_claude_archive_empty_transcript_writes_nothing(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    archive_dir = tmp_path / "archive"
    handler = TranscriptArchiveHandler(archive_dir=archive_dir)

    await handler.on_pre_compact(
        CompactionEvent(transcript_path=str(transcript), session_id="empty")
    )
    assert list(archive_dir.glob("*.md")) == []


async def test_claude_archive_handles_garbage_lines(tmp_path: Path) -> None:
    """Invalid JSON lines in the middle must not derail valid ones before/after.
    This is the bug class that prompted the original try/except on each line."""
    transcript = tmp_path / "session.jsonl"
    content = (
        json.dumps(_claude_entry_user("valid-1"))
        + "\nnot-valid-json\n"
        + json.dumps(_claude_entry_assistant("valid-2"))
    )
    transcript.write_text(content)
    archive_dir = tmp_path / "archive"
    handler = TranscriptArchiveHandler(archive_dir=archive_dir)

    await handler.on_pre_compact(
        CompactionEvent(transcript_path=str(transcript), session_id=None)
    )
    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "valid-1" in body
    assert "valid-2" in body


async def test_claude_archive_long_message_truncated(tmp_path: Path) -> None:
    """Messages longer than 2000 chars get an ellipsis appended so the
    archive stays readable on disk. Mutation: forgetting the '...' marker
    would be a silent change."""
    long_text = "x" * 2500
    transcript = tmp_path / "s.jsonl"
    _write_transcript(transcript, [_claude_entry_user(long_text)])
    handler = TranscriptArchiveHandler(archive_dir=tmp_path / "arc")
    await handler.on_pre_compact(
        CompactionEvent(transcript_path=str(transcript), session_id=None)
    )
    body = (tmp_path / "arc").glob("*.md").__next__().read_text()
    assert ("x" * 2000) + "..." in body
    assert "x" * 2500 not in body


async def test_claude_archive_missing_file_returns_quietly(tmp_path: Path) -> None:
    handler = TranscriptArchiveHandler(archive_dir=tmp_path / "arc")
    # Must not raise even though the path does not exist.
    await handler.on_pre_compact(
        CompactionEvent(
            transcript_path=str(tmp_path / "nope.jsonl"), session_id="x"
        )
    )
    assert not (tmp_path / "arc").exists()


# ---------------------------------------------------------------------------
# Pi branch
# ---------------------------------------------------------------------------


async def test_pi_archive_from_messages(tmp_path: Path) -> None:
    msgs = [
        _FakePiMessage(role="user", content=[_FakePiBlock(text="hello")]),
        _FakePiMessage(
            role="assistant", content=[_FakePiBlock(text="hi there")]
        ),
        _FakePiMessage(role="toolResult", content=[_FakePiBlock(text="ignored")]),
        _FakePiMessage(role="user", content=[_FakePiBlock(text="bye")]),
    ]
    archive_dir = tmp_path / "arc"
    handler = TranscriptArchiveHandler(
        assistant_name="Skynet", archive_dir=archive_dir
    )
    await handler.on_pre_compact(CompactionEvent(messages=msgs))

    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "**User**: hello" in body
    assert "**Skynet**: hi there" in body
    assert "**User**: bye" in body
    # Tool results are not archived.
    assert "ignored" not in body


async def test_pi_archive_with_string_content(tmp_path: Path) -> None:
    """Pi UserMessage.content can be either list or str — cover the str path."""
    msgs = [_FakePiMessage(role="user", content="raw string form")]
    archive_dir = tmp_path / "arc"
    handler = TranscriptArchiveHandler(archive_dir=archive_dir)
    await handler.on_pre_compact(CompactionEvent(messages=msgs))
    files = list(archive_dir.glob("*.md"))
    assert len(files) == 1
    assert "**User**: raw string form" in files[0].read_text()


async def test_pi_archive_empty_messages_skipped(tmp_path: Path) -> None:
    msgs = [
        _FakePiMessage(role="user", content=[]),
        _FakePiMessage(role="toolResult", content="trash"),
    ]
    archive_dir = tmp_path / "arc"
    handler = TranscriptArchiveHandler(archive_dir=archive_dir)
    await handler.on_pre_compact(CompactionEvent(messages=msgs))
    # No archivable content -> no file.
    assert not archive_dir.exists() or list(archive_dir.glob("*.md")) == []


async def test_event_without_transcript_or_messages_no_op(tmp_path: Path) -> None:
    handler = TranscriptArchiveHandler(archive_dir=tmp_path / "arc")
    # Must not raise; no file.
    await handler.on_pre_compact(CompactionEvent())
    assert not (tmp_path / "arc").exists()


# ---------------------------------------------------------------------------
# _sanitize_filename — pure function, edge-case coverage
# ---------------------------------------------------------------------------


def test_sanitize_filename_collapses_runs_and_trims() -> None:
    assert _sanitize_filename("Hello,     World!") == "hello-world"
    assert _sanitize_filename("___foo bar___") == "foo-bar"
    # Non-ASCII gets stripped down to whatever is left
    assert _sanitize_filename("日本語のtitle") == "title"
    # All-punctuation survives as empty string (caller falls back)
    assert _sanitize_filename("!!!") == ""
    # Length capped at 50
    out = _sanitize_filename("a" * 100)
    assert len(out) == 50 and set(out) == {"a"}
