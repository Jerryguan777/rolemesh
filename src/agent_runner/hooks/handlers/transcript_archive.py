"""Archive conversation transcripts to Markdown before compaction.

Branches on CompactionEvent payload shape:
  - Claude backend provides transcript_path (JSONL on disk) + session_id.
    We read the file, find a summary in sessions-index.json if available,
    and write a Markdown archive under /workspace/group/conversations/.
  - Pi backend provides a list of AgentMessage objects (from
    CompactionPreparation.messages_to_summarize). We walk them in-memory
    and produce the same Markdown shape, with a timestamp-derived
    filename since Pi does not carry a session summary.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..events import CompactionEvent

_log = logging.getLogger(__name__)

_ARCHIVE_DIR = Path("/workspace/group/conversations")
_MAX_MESSAGE_CHARS = 2000


def _sanitize_filename(summary: str) -> str:
    name = summary.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    return name[:50]


def _generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def _parse_claude_transcript(content: str) -> list[tuple[str, str]]:
    """Parse a Claude JSONL transcript into (role, text) pairs."""
    messages: list[tuple[str, str]] = []
    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            entry_type = entry.get("type")
            message = entry.get("message")
            if not message:
                continue
            if entry_type == "user":
                msg_content = message.get("content")
                if isinstance(msg_content, str):
                    text = msg_content
                elif isinstance(msg_content, list):
                    text = "".join(c.get("text", "") for c in msg_content)
                else:
                    text = ""
                if text:
                    messages.append(("user", text))
            elif entry_type == "assistant":
                blocks = message.get("content") or []
                text_parts = [
                    c.get("text", "")
                    for c in blocks
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                text = "".join(text_parts)
                if text:
                    messages.append(("assistant", text))
        except (AttributeError, TypeError):
            continue
    return messages


def _get_session_summary(session_id: str, transcript_path: str) -> str | None:
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"
    if not index_path.exists():
        return None
    try:
        index_data = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for entry in index_data.get("entries", []):
        if entry.get("sessionId") == session_id:
            summary = entry.get("summary")
            if isinstance(summary, str):
                return summary
    return None


def _truncate(text: str) -> str:
    if len(text) <= _MAX_MESSAGE_CHARS:
        return text
    return text[:_MAX_MESSAGE_CHARS] + "..."


def _render_markdown(
    summary: str | None,
    messages: list[tuple[str, str]],
    assistant_name: str | None,
) -> str:
    now = datetime.now()
    date_str = now.strftime("%b %-d, %-I:%M %p")
    lines: list[str] = [
        f"# {summary or 'Conversation'}",
        "",
        f"Archived: {date_str}",
        "",
        "---",
        "",
    ]
    for role, text in messages:
        sender = "User" if role == "user" else (assistant_name or "Assistant")
        lines.append(f"**{sender}**: {_truncate(text)}")
        lines.append("")
    return "\n".join(lines)


def _write_archive(
    *,
    summary: str | None,
    messages: list[tuple[str, str]],
    assistant_name: str | None,
    archive_dir: Path,
) -> Path | None:
    if not messages:
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = _sanitize_filename(summary) if summary else _generate_fallback_name()
    if not name:
        name = _generate_fallback_name()
    date = datetime.now().strftime("%Y-%m-%d")
    filepath = archive_dir / f"{date}-{name}.md"
    filepath.write_text(_render_markdown(summary, messages, assistant_name))
    return filepath


def _pi_message_to_text(message: Any) -> tuple[str, str] | None:
    """Convert one Pi AgentMessage into (role, text).

    Uses duck typing on role so we do not import pi.ai types from the
    hooks package (keeps hooks/ free of backend dependencies). Returns
    None for messages whose role we do not archive (tool results,
    unknown roles).
    """
    role = getattr(message, "role", None)
    if role not in ("user", "assistant"):
        return None
    content = getattr(message, "content", None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text_part = getattr(block, "text", None)
            if isinstance(text_part, str):
                parts.append(text_part)
        text = "".join(parts)
    else:
        text = ""
    if not text:
        return None
    return (role, text)


def _messages_from_pi(messages: list[Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for msg in messages:
        pair = _pi_message_to_text(msg)
        if pair is not None:
            out.append(pair)
    return out


class TranscriptArchiveHandler:
    """PreCompact handler that archives conversations to Markdown files."""

    def __init__(
        self,
        assistant_name: str | None = None,
        archive_dir: Path | None = None,
    ) -> None:
        self._assistant_name = assistant_name
        self._archive_dir = archive_dir or _ARCHIVE_DIR

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        try:
            if event.transcript_path:
                self._archive_from_transcript_file(
                    event.transcript_path, event.session_id
                )
            elif event.messages:
                self._archive_from_pi_messages(event.messages)
        except Exception as exc:  # noqa: BLE001 — archiving is best-effort
            # Archiving is best-effort; never let it interrupt compaction.
            _log.warning("Failed to archive transcript: %s", exc)
            print(
                f"[transcript-archive] Failed to archive: {exc}",
                file=sys.stderr,
                flush=True,
            )

    def _archive_from_transcript_file(
        self, transcript_path: str, session_id: str | None
    ) -> None:
        path = Path(transcript_path)
        if not path.exists():
            return
        content = path.read_text()
        messages = _parse_claude_transcript(content)
        if not messages:
            return
        summary = (
            _get_session_summary(session_id, transcript_path) if session_id else None
        )
        filepath = _write_archive(
            summary=summary,
            messages=messages,
            assistant_name=self._assistant_name,
            archive_dir=self._archive_dir,
        )
        if filepath is not None:
            print(
                f"[transcript-archive] Archived conversation to {filepath}",
                file=sys.stderr,
                flush=True,
            )

    def _archive_from_pi_messages(self, messages: list[Any]) -> None:
        pairs = _messages_from_pi(messages)
        if not pairs:
            return
        filepath = _write_archive(
            summary=None,
            messages=pairs,
            assistant_name=self._assistant_name,
            archive_dir=self._archive_dir,
        )
        if filepath is not None:
            print(
                f"[transcript-archive] Archived Pi conversation to {filepath}",
                file=sys.stderr,
                flush=True,
            )
