"""Session manager — Python port of packages/coding-agent/src/core/session-manager.ts.

Manages conversation sessions as append-only trees stored in JSONL files.
Each entry has an id and parentId forming a tree. The "leaf" pointer tracks
the current position. Appending creates a child of the current leaf.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pi.ai.types import (
    ImageContent,
    TextContent,
    deserialize_message,
    serialize_message,
)
from pi.coding_agent.core.messages import (
    BashExecutionMessage,
    CustomMessage,
    SessionMessage,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CURRENT_SESSION_VERSION = 3


# ---------------------------------------------------------------------------
# Entry dataclasses (discriminated union)
# ---------------------------------------------------------------------------


@dataclass
class SessionHeader:
    """JSONL header line for a session file."""

    type: Literal["session"] = "session"
    version: int | None = None
    id: str = ""
    timestamp: str = ""
    cwd: str = ""
    parent_session: str | None = None


@dataclass
class NewSessionOptions:
    """Options for creating a new session."""

    parent_session: str | None = None


@dataclass
class SessionEntryBase:
    """Common fields for all session entries."""

    id: str = ""
    parent_id: str | None = None
    timestamp: str = ""


@dataclass
class SessionMessageEntry(SessionEntryBase):
    """A regular agent message stored in the session."""

    type: Literal["message"] = "message"
    message: Any = None  # AgentMessage (serialized/deserialized)


@dataclass
class ThinkingLevelChangeEntry(SessionEntryBase):
    """Records a change in thinking level."""

    type: Literal["thinking_level_change"] = "thinking_level_change"
    thinking_level: str = ""


@dataclass
class ModelChangeEntry(SessionEntryBase):
    """Records a model switch."""

    type: Literal["model_change"] = "model_change"
    provider: str = ""
    model_id: str = ""


@dataclass
class CompactionEntry(SessionEntryBase):
    """Records a context compaction."""

    type: Literal["compaction"] = "compaction"
    summary: str = ""
    first_kept_entry_id: str = ""
    tokens_before: int = 0
    details: Any = None
    from_hook: bool | None = None


@dataclass
class BranchSummaryEntry(SessionEntryBase):
    """Records a branch summary when navigating back from a branch."""

    type: Literal["branch_summary"] = "branch_summary"
    from_id: str = ""
    summary: str = ""
    details: Any = None
    from_hook: bool | None = None


@dataclass
class CustomEntry(SessionEntryBase):
    """Extension-specific persistent data (not sent to LLM)."""

    type: Literal["custom"] = "custom"
    custom_type: str = ""
    data: Any = None


@dataclass
class CustomMessageEntry(SessionEntryBase):
    """Extension message that participates in LLM context."""

    type: Literal["custom_message"] = "custom_message"
    custom_type: str = ""
    content: str | list[TextContent | ImageContent] = ""
    display: bool = True
    details: Any = None


@dataclass
class LabelEntry(SessionEntryBase):
    """User-defined bookmark on an entry."""

    type: Literal["label"] = "label"
    target_id: str = ""
    label: str | None = None


@dataclass
class SessionInfoEntry(SessionEntryBase):
    """Session metadata (e.g., display name)."""

    type: Literal["session_info"] = "session_info"
    name: str | None = None


# Union of all session entry types
SessionEntry = (
    SessionMessageEntry
    | ThinkingLevelChangeEntry
    | ModelChangeEntry
    | CompactionEntry
    | BranchSummaryEntry
    | CustomEntry
    | CustomMessageEntry
    | LabelEntry
    | SessionInfoEntry
)

FileEntry = SessionHeader | SessionEntry

SessionListProgress = Callable[[int, int], None]


@dataclass
class SessionTreeNode:
    """Node in the session tree for getTree()."""

    entry: SessionEntry
    children: list[SessionTreeNode] = field(default_factory=list)
    label: str | None = None


@dataclass
class SessionContext:
    """The resolved context to pass to the LLM."""

    messages: list[SessionMessage] = field(default_factory=list)
    thinking_level: str = "off"
    model: dict[str, str] | None = None  # {"provider": ..., "model_id": ...}


@dataclass
class SessionInfo:
    """Metadata about a session file."""

    path: str = ""
    id: str = ""
    cwd: str = ""
    name: str | None = None
    parent_session_path: str | None = None
    created: datetime = field(default_factory=datetime.now)
    modified: datetime = field(default_factory=datetime.now)
    message_count: int = 0
    first_message: str = ""
    all_messages_text: str = ""


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(tz=UTC).isoformat()


def _generate_id(existing: set[str] | dict[str, Any]) -> str:
    """Generate a unique 8-char hex ID, collision-checked against existing."""
    ids = existing if isinstance(existing, set) else set(existing.keys())
    for _ in range(100):
        candidate = str(uuid.uuid4())[:8]
        if candidate not in ids:
            return candidate
    return str(uuid.uuid4())


def _serialize_entry(entry: FileEntry) -> dict[str, Any]:
    """Serialize a FileEntry to a plain dict for JSON output."""
    if isinstance(entry, SessionHeader):
        d: dict[str, Any] = {
            "type": "session",
            "id": entry.id,
            "timestamp": entry.timestamp,
            "cwd": entry.cwd,
        }
        if entry.version is not None:
            d["version"] = entry.version
        if entry.parent_session is not None:
            d["parentSession"] = entry.parent_session
        return d

    if isinstance(entry, SessionMessageEntry):
        msg = entry.message
        if isinstance(msg, BashExecutionMessage):
            serialized_msg: dict[str, Any] = {
                "role": "bashExecution",
                "command": msg.command,
                "output": msg.stdout,  # store combined output as 'output' for TS compat
                "exitCode": msg.exit_code,
                "cancelled": msg.cancelled,
                "truncated": msg.truncated,
                "timestamp": msg.timestamp,
            }
            if msg.full_output_path is not None:
                serialized_msg["fullOutputPath"] = msg.full_output_path
            if msg.exclude_from_context is not None:
                serialized_msg["excludeFromContext"] = msg.exclude_from_context
        elif isinstance(msg, CustomMessage):
            content: Any
            if isinstance(msg.content, str):
                content = msg.content
            else:
                from pi.ai.types import serialize_content_block

                content = [serialize_content_block(b) for b in msg.content]
            serialized_msg = {
                "role": "custom",
                "customType": msg.custom_type,
                "content": content,
                "display": msg.display,
                "timestamp": msg.timestamp,
            }
            if msg.details is not None:
                serialized_msg["details"] = msg.details
        else:
            # Standard LLM message
            serialized_msg = serialize_message(msg)

        return {
            "type": "message",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "message": serialized_msg,
        }

    if isinstance(entry, ThinkingLevelChangeEntry):
        return {
            "type": "thinking_level_change",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "thinkingLevel": entry.thinking_level,
        }

    if isinstance(entry, ModelChangeEntry):
        return {
            "type": "model_change",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "provider": entry.provider,
            "modelId": entry.model_id,
        }

    if isinstance(entry, CompactionEntry):
        d = {
            "type": "compaction",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "summary": entry.summary,
            "firstKeptEntryId": entry.first_kept_entry_id,
            "tokensBefore": entry.tokens_before,
        }
        if entry.details is not None:
            d["details"] = entry.details
        if entry.from_hook is not None:
            d["fromHook"] = entry.from_hook
        return d

    if isinstance(entry, BranchSummaryEntry):
        d = {
            "type": "branch_summary",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "fromId": entry.from_id,
            "summary": entry.summary,
        }
        if entry.details is not None:
            d["details"] = entry.details
        if entry.from_hook is not None:
            d["fromHook"] = entry.from_hook
        return d

    if isinstance(entry, CustomEntry):
        d = {
            "type": "custom",
            "customType": entry.custom_type,
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
        }
        if entry.data is not None:
            d["data"] = entry.data
        return d

    if isinstance(entry, CustomMessageEntry):
        if isinstance(entry.content, str):
            content = entry.content
        else:
            from pi.ai.types import serialize_content_block

            content = [serialize_content_block(b) for b in entry.content]
        d = {
            "type": "custom_message",
            "customType": entry.custom_type,
            "content": content,
            "display": entry.display,
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
        }
        if entry.details is not None:
            d["details"] = entry.details
        return d

    if isinstance(entry, LabelEntry):
        d = {
            "type": "label",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
            "targetId": entry.target_id,
            "label": entry.label,
        }
        return d

    if isinstance(entry, SessionInfoEntry):
        d = {
            "type": "session_info",
            "id": entry.id,
            "parentId": entry.parent_id,
            "timestamp": entry.timestamp,
        }
        if entry.name is not None:
            d["name"] = entry.name
        return d

    raise ValueError(f"Unknown entry type: {type(entry)}")  # pragma: no cover


def _deserialize_message_data(data: dict[str, Any]) -> SessionMessage:
    """Deserialize a message dict from session storage."""
    role = data.get("role", "")

    if role == "bashExecution":
        return BashExecutionMessage(
            command=data.get("command", ""),
            stdout=data.get("output", ""),  # TS stores as 'output'
            stderr="",
            exit_code=data.get("exitCode"),
            cancelled=data.get("cancelled", False),
            truncated=data.get("truncated", False),
            full_output_path=data.get("fullOutputPath"),
            timestamp=data.get("timestamp", 0),
            exclude_from_context=data.get("excludeFromContext"),
        )

    if role == "custom":
        raw_content = data.get("content", "")
        if isinstance(raw_content, str):
            content: str | list[TextContent | ImageContent] = raw_content
        else:
            from pi.ai.types import ImageContent as IC
            from pi.ai.types import TextContent as TC
            from pi.ai.types import deserialize_content_block

            content = []
            for b in raw_content:
                block = deserialize_content_block(b)
                if isinstance(block, (TC, IC)):
                    content.append(block)
        return CustomMessage(
            custom_type=data.get("customType", ""),
            content=content,
            display=data.get("display", True),
            details=data.get("details"),
            timestamp=data.get("timestamp", 0),
        )

    # Standard LLM messages
    return deserialize_message(data)


def _deserialize_entry(data: dict[str, Any]) -> FileEntry | None:
    """Deserialize a dict from JSONL to a typed FileEntry. Returns None on unknown type."""
    entry_type = data.get("type", "")

    if entry_type == "session":
        return SessionHeader(
            version=data.get("version"),
            id=data.get("id", ""),
            timestamp=data.get("timestamp", ""),
            cwd=data.get("cwd", ""),
            parent_session=data.get("parentSession"),
        )

    base_id = data.get("id", "")
    base_parent_id = data.get("parentId")
    base_timestamp = data.get("timestamp", "")

    if entry_type == "message":
        msg_data = data.get("message", {})
        msg = _deserialize_message_data(msg_data)
        return SessionMessageEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            message=msg,
        )

    if entry_type == "thinking_level_change":
        return ThinkingLevelChangeEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            thinking_level=data.get("thinkingLevel", "off"),
        )

    if entry_type == "model_change":
        return ModelChangeEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            provider=data.get("provider", ""),
            model_id=data.get("modelId", ""),
        )

    if entry_type == "compaction":
        return CompactionEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            summary=data.get("summary", ""),
            first_kept_entry_id=data.get("firstKeptEntryId", ""),
            tokens_before=data.get("tokensBefore", 0),
            details=data.get("details"),
            from_hook=data.get("fromHook"),
        )

    if entry_type == "branch_summary":
        return BranchSummaryEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            from_id=data.get("fromId", ""),
            summary=data.get("summary", ""),
            details=data.get("details"),
            from_hook=data.get("fromHook"),
        )

    if entry_type == "custom":
        return CustomEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            custom_type=data.get("customType", ""),
            data=data.get("data"),
        )

    if entry_type == "custom_message":
        raw_content = data.get("content", "")
        if isinstance(raw_content, str):
            content: str | list[TextContent | ImageContent] = raw_content
        else:
            from pi.ai.types import ImageContent as IC
            from pi.ai.types import TextContent as TC
            from pi.ai.types import deserialize_content_block

            content = []
            for b in raw_content:
                block = deserialize_content_block(b)
                if isinstance(block, (TC, IC)):
                    content.append(block)
        return CustomMessageEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            custom_type=data.get("customType", ""),
            content=content,
            display=data.get("display", True),
            details=data.get("details"),
        )

    if entry_type == "label":
        return LabelEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            target_id=data.get("targetId", ""),
            label=data.get("label"),
        )

    if entry_type == "session_info":
        return SessionInfoEntry(
            id=base_id,
            parent_id=base_parent_id,
            timestamp=base_timestamp,
            name=data.get("name"),
        )

    return None


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def _migrate_v1_to_v2(entries: list[FileEntry]) -> None:
    """Migrate v1 sessions (no id/parentId) to v2. Mutates in-place."""
    existing_ids: set[str] = set()
    prev_id: str | None = None

    for entry in entries:
        if isinstance(entry, SessionHeader):
            entry.version = 2
            continue

        if not isinstance(entry, SessionEntryBase):
            continue

        entry.id = _generate_id(existing_ids)
        existing_ids.add(entry.id)
        entry.parent_id = prev_id
        prev_id = entry.id


def _migrate_v2_to_v3(entries: list[FileEntry]) -> None:
    """Migrate v2 sessions (hookMessage role) to v3. Mutates in-place."""
    for entry in entries:
        if isinstance(entry, SessionHeader):
            entry.version = 3
            continue
        if isinstance(entry, SessionMessageEntry):
            msg = entry.message
            # Replace deprecated hookMessage role with a proper CustomMessage
            if hasattr(msg, "role") and msg.role == "hookMessage":
                entry.message = CustomMessage(
                    custom_type="hookMessage",
                    content=getattr(msg, "content", ""),
                    display=getattr(msg, "display", True),
                    details=getattr(msg, "details", None),
                    timestamp=getattr(msg, "timestamp", 0),
                )


def _migrate_to_current_version(entries: list[FileEntry]) -> bool:
    """Run all pending migrations. Returns True if any migration was applied."""
    header = next((e for e in entries if isinstance(e, SessionHeader)), None)
    version = header.version if header and header.version is not None else 1

    if version >= CURRENT_SESSION_VERSION:
        return False

    if version < 2:
        _migrate_v1_to_v2(entries)
    if version < 3:
        _migrate_v2_to_v3(entries)

    return True


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def parse_session_entries(content: str) -> list[FileEntry]:
    """Parse JSONL content into a list of FileEntry objects."""
    entries: list[FileEntry] = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            entry = _deserialize_entry(data)
            if entry is not None:
                entries.append(entry)
        except Exception:
            pass  # Skip malformed lines
    return entries


def migrate_session_entries(entries: list[FileEntry]) -> None:
    """Run migrations on entries list (exported for testing)."""
    _migrate_to_current_version(entries)


def load_entries_from_file(file_path: str) -> list[FileEntry]:
    """Load and parse JSONL entries from a session file."""
    path = Path(file_path)
    if not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    entries = parse_session_entries(content)

    if not entries:
        return entries

    # Validate session header
    header = entries[0]
    if not isinstance(header, SessionHeader) or not header.id:
        return []

    return entries


def _is_valid_session_file(file_path: str) -> bool:
    """Check if a file starts with a valid session header."""
    try:
        with open(file_path, encoding="utf-8") as f:
            first_line = f.readline().strip()
        data = json.loads(first_line)
        return data.get("type") == "session" and isinstance(data.get("id"), str)
    except (OSError, json.JSONDecodeError):
        return False


def find_most_recent_session(session_dir: str) -> str | None:
    """Find the most recently modified valid session file in a directory."""
    dir_path = Path(session_dir)
    if not dir_path.exists():
        return None

    try:
        files = [(p, p.stat().st_mtime) for p in dir_path.glob("*.jsonl") if _is_valid_session_file(str(p))]
        if not files:
            return None
        files.sort(key=lambda x: x[1], reverse=True)
        return str(files[0][0])
    except OSError:
        return None


def _get_default_session_dir(cwd: str) -> str:
    """Get the default session directory for a working directory."""
    safe_path = "--" + cwd.lstrip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    agent_dir = _get_agent_dir()
    session_dir = os.path.join(agent_dir, "sessions", safe_path)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def _get_agent_dir() -> str:
    """Return the agent configuration directory (~/.pi/agent or override via env)."""
    env_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if env_dir:
        if env_dir == "~":
            return os.path.expanduser("~")
        if env_dir.startswith("~/"):
            return os.path.expanduser(env_dir)
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".pi", "agent")


# ---------------------------------------------------------------------------
# buildSessionContext standalone function
# ---------------------------------------------------------------------------


def build_session_context(
    entries: list[SessionEntry],
    leaf_id: str | None = None,
    by_id: dict[str, SessionEntry] | None = None,
) -> SessionContext:
    """Build the session context from entries using tree traversal.

    Handles compaction and branch summaries along the path from root to leaf.

    Args:
        entries: All session entries.
        leaf_id: The leaf entry ID to walk from. If None, uses last entry.
        by_id: Optional pre-built ID index.

    Returns:
        SessionContext with messages, thinking level, and model info.
    """
    if by_id is None:
        by_id = {e.id: e for e in entries}

    # Find leaf entry
    leaf: SessionEntry | None = None
    if leaf_id is not None:
        leaf = by_id.get(leaf_id)
    if leaf is None:
        leaf = entries[-1] if entries else None

    if leaf is None:
        return SessionContext()

    # Walk from leaf to root, building path
    path: list[SessionEntry] = []
    current: SessionEntry | None = leaf
    while current is not None:
        path.insert(0, current)
        current = by_id.get(current.parent_id) if current.parent_id else None

    # Extract settings and find compaction
    thinking_level = "off"
    model: dict[str, str] | None = None
    compaction: CompactionEntry | None = None

    for entry in path:
        if isinstance(entry, ThinkingLevelChangeEntry):
            thinking_level = entry.thinking_level
        elif isinstance(entry, ModelChangeEntry):
            model = {"provider": entry.provider, "model_id": entry.model_id}
        elif isinstance(entry, SessionMessageEntry) and hasattr(entry.message, "role"):
            msg = entry.message
            if getattr(msg, "role", "") == "assistant":
                provider = getattr(msg, "provider", "")
                model_id = getattr(msg, "model", "")
                if provider or model_id:
                    model = {"provider": provider, "model_id": model_id}
        elif isinstance(entry, CompactionEntry):
            compaction = entry

    # Build messages list
    messages: list[SessionMessage] = []

    def _append_message(entry: SessionEntry) -> None:
        if isinstance(entry, SessionMessageEntry):
            messages.append(entry.message)
        elif isinstance(entry, CustomMessageEntry):
            messages.append(
                create_custom_message(
                    entry.custom_type,
                    entry.content,
                    entry.display,
                    entry.details,
                    entry.timestamp,
                )
            )
        elif isinstance(entry, BranchSummaryEntry) and entry.summary:
            messages.append(create_branch_summary_message(entry.summary, entry.from_id, entry.timestamp))

    if compaction is not None:
        # Emit compaction summary first
        messages.append(
            create_compaction_summary_message(compaction.summary, compaction.tokens_before, compaction.timestamp)
        )

        # Find compaction index in path
        compaction_idx = next(
            (i for i, e in enumerate(path) if isinstance(e, CompactionEntry) and e.id == compaction.id),
            -1,
        )

        # Emit kept messages (before compaction, starting from first_kept_entry_id)
        found_first_kept = False
        for i in range(compaction_idx):
            entry = path[i]
            if entry.id == compaction.first_kept_entry_id:
                found_first_kept = True
            if found_first_kept:
                _append_message(entry)

        # Emit messages after compaction
        for i in range(compaction_idx + 1, len(path)):
            _append_message(path[i])
    else:
        for entry in path:
            _append_message(entry)

    return SessionContext(messages=messages, thinking_level=thinking_level, model=model)


def get_latest_compaction_entry(entries: list[SessionEntry]) -> CompactionEntry | None:
    """Return the most recent CompactionEntry in entries, or None."""
    for entry in reversed(entries):
        if isinstance(entry, CompactionEntry):
            return entry
    return None


# ---------------------------------------------------------------------------
# SessionManager class
# ---------------------------------------------------------------------------


class SessionManager:
    """Manages conversation sessions as append-only trees stored in JSONL files.

    Use the class methods create(), open(), continue_recent(), or in_memory()
    to construct instances.
    """

    def __init__(
        self,
        cwd: str,
        session_dir: str,
        session_file: str | None,
        persist: bool,
    ) -> None:
        self._cwd = cwd
        self._session_dir = session_dir
        self._persist = persist
        self._flushed = False
        self._file_entries: list[FileEntry] = []
        self._by_id: dict[str, SessionEntry] = {}
        self._labels_by_id: dict[str, str] = {}
        self._leaf_id: str | None = None
        self._session_id: str = ""
        self._session_file: str | None = None

        if persist and session_dir:
            os.makedirs(session_dir, exist_ok=True)

        if session_file is not None:
            self.set_session_file(session_file)
        else:
            self.new_session()

    # -------------------------------------------------------------------------
    # Session lifecycle
    # -------------------------------------------------------------------------

    def set_session_file(self, session_file: str) -> None:
        """Load or create a session from the given file path."""
        self._session_file = str(Path(session_file).resolve())
        path = Path(self._session_file)

        if path.exists():
            self._file_entries = load_entries_from_file(self._session_file)

            if not self._file_entries:
                # Corrupted or empty — start fresh at that path
                explicit_path = self._session_file
                self.new_session()
                self._session_file = explicit_path
                self._rewrite_file()
                self._flushed = True
                return

            header = next((e for e in self._file_entries if isinstance(e, SessionHeader)), None)
            self._session_id = header.id if header else str(uuid.uuid4())

            if _migrate_to_current_version(self._file_entries):
                self._rewrite_file()

            self._build_index()
            self._flushed = True
        else:
            explicit_path = self._session_file
            self.new_session()
            self._session_file = explicit_path

    def new_session(self, options: NewSessionOptions | None = None) -> str | None:
        """Start a new session (clears all entries). Returns the session file path."""
        self._session_id = str(uuid.uuid4())
        timestamp = _now_iso()
        header = SessionHeader(
            version=CURRENT_SESSION_VERSION,
            id=self._session_id,
            timestamp=timestamp,
            cwd=self._cwd,
            parent_session=options.parent_session if options else None,
        )
        self._file_entries = [header]
        self._by_id.clear()
        self._labels_by_id.clear()
        self._leaf_id = None
        self._flushed = False

        if self._persist:
            file_timestamp = timestamp.replace(":", "-").replace(".", "-")
            self._session_file = os.path.join(self.get_session_dir(), f"{file_timestamp}_{self._session_id}.jsonl")

        return self._session_file

    def _build_index(self) -> None:
        """Rebuild byId and labelsById from file entries."""
        self._by_id.clear()
        self._labels_by_id.clear()
        self._leaf_id = None

        for entry in self._file_entries:
            if isinstance(entry, SessionHeader):
                continue
            self._by_id[entry.id] = entry
            self._leaf_id = entry.id
            if isinstance(entry, LabelEntry):
                if entry.label:
                    self._labels_by_id[entry.target_id] = entry.label
                else:
                    self._labels_by_id.pop(entry.target_id, None)

    def _rewrite_file(self) -> None:
        """Rewrite the entire session file from in-memory entries."""
        if not self._persist or not self._session_file:
            return
        content = "\n".join(json.dumps(_serialize_entry(e)) for e in self._file_entries) + "\n"
        Path(self._session_file).write_text(content, encoding="utf-8")

    def is_persisted(self) -> bool:
        """Return True if this session manager persists to disk."""
        return self._persist

    def get_cwd(self) -> str:
        """Return the working directory associated with this session."""
        return self._cwd

    def get_session_dir(self) -> str:
        """Return the session directory path."""
        return self._session_dir

    def get_session_id(self) -> str:
        """Return the current session UUID."""
        return self._session_id

    def get_session_file(self) -> str | None:
        """Return the current session file path, or None if not persisting."""
        return self._session_file

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def _persist_entry(self, entry: SessionEntry) -> None:
        """Append entry to the session file (lazy: waits for first assistant message)."""
        if not self._persist or not self._session_file:
            return

        has_assistant = any(
            isinstance(e, SessionMessageEntry)
            and hasattr(e.message, "role")
            and getattr(e.message, "role", "") == "assistant"
            for e in self._file_entries
        )

        if not has_assistant:
            self._flushed = False
            return

        if not self._flushed:
            # Flush all buffered entries
            with open(self._session_file, "a", encoding="utf-8") as f:
                for e in self._file_entries:
                    f.write(json.dumps(_serialize_entry(e)) + "\n")
            self._flushed = True
        else:
            with open(self._session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(_serialize_entry(entry)) + "\n")

    def _append_entry(self, entry: SessionEntry) -> None:
        """Add entry to in-memory list, index, and persist."""
        self._file_entries.append(entry)
        self._by_id[entry.id] = entry
        self._leaf_id = entry.id
        self._persist_entry(entry)

    # -------------------------------------------------------------------------
    # Append methods
    # -------------------------------------------------------------------------

    def append_message(
        self,
        message: SessionMessage,
    ) -> str:
        """Append a message as a child of current leaf. Returns entry ID."""
        entry = SessionMessageEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            message=message,
        )
        self._append_entry(entry)
        return entry.id

    def append_thinking_level_change(self, thinking_level: str) -> str:
        """Append a thinking level change entry. Returns entry ID."""
        entry = ThinkingLevelChangeEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            thinking_level=thinking_level,
        )
        self._append_entry(entry)
        return entry.id

    def append_model_change(self, provider: str, model_id: str) -> str:
        """Append a model change entry. Returns entry ID."""
        entry = ModelChangeEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            provider=provider,
            model_id=model_id,
        )
        self._append_entry(entry)
        return entry.id

    def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
        details: Any = None,
        from_hook: bool | None = None,
    ) -> str:
        """Append a compaction entry. Returns entry ID."""
        entry = CompactionEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            summary=summary,
            first_kept_entry_id=first_kept_entry_id,
            tokens_before=tokens_before,
            details=details,
            from_hook=from_hook,
        )
        self._append_entry(entry)
        return entry.id

    def append_compaction_entry(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
        details: Any = None,
        from_hook: bool | None = None,
    ) -> str:
        """Alias for append_compaction (matches shared interface contract)."""
        return self.append_compaction(summary, first_kept_entry_id, tokens_before, details, from_hook)

    def append_custom_entry(self, custom_type: str, data: Any = None) -> str:
        """Append a custom extension entry. Returns entry ID."""
        entry = CustomEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            custom_type=custom_type,
            data=data,
        )
        self._append_entry(entry)
        return entry.id

    def append_session_info(self, name: str) -> str:
        """Append a session info entry (e.g., display name). Returns entry ID."""
        entry = SessionInfoEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            name=name.strip(),
        )
        self._append_entry(entry)
        return entry.id

    def append_custom_message_entry(
        self,
        custom_type: str,
        content: str | list[TextContent | ImageContent],
        display: bool,
        details: Any = None,
    ) -> str:
        """Append a custom message entry that participates in LLM context. Returns entry ID."""
        entry = CustomMessageEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            custom_type=custom_type,
            content=content,
            display=display,
            details=details,
        )
        self._append_entry(entry)
        return entry.id

    def append_label_change(self, target_id: str, label: str | None) -> str:
        """Set or clear a label on an entry. Returns entry ID."""
        if target_id not in self._by_id:
            raise ValueError(f"Entry {target_id} not found")

        entry = LabelEntry(
            id=_generate_id(self._by_id),
            parent_id=self._leaf_id,
            timestamp=_now_iso(),
            target_id=target_id,
            label=label,
        )
        self._append_entry(entry)

        if label:
            self._labels_by_id[target_id] = label
        else:
            self._labels_by_id.pop(target_id, None)

        return entry.id

    def get_session_name(self) -> str | None:
        """Return the most recent session display name, or None."""
        for entry in reversed(self.get_entries()):
            if isinstance(entry, SessionInfoEntry) and entry.name:
                return entry.name
        return None

    # -------------------------------------------------------------------------
    # Tree traversal
    # -------------------------------------------------------------------------

    def get_leaf_id(self) -> str | None:
        """Return the current leaf entry ID."""
        return self._leaf_id

    def get_leaf_entry(self) -> SessionEntry | None:
        """Return the current leaf entry."""
        return self._by_id.get(self._leaf_id) if self._leaf_id else None

    def get_entry(self, entry_id: str) -> SessionEntry | None:
        """Return a session entry by ID."""
        return self._by_id.get(entry_id)

    def get_children(self, parent_id: str) -> list[SessionEntry]:
        """Return all direct children of an entry."""
        return [e for e in self._by_id.values() if e.parent_id == parent_id]

    def get_label(self, entry_id: str) -> str | None:
        """Return the label for an entry, if any."""
        return self._labels_by_id.get(entry_id)

    def get_branch(self, from_id: str | None = None) -> list[SessionEntry]:
        """Walk from an entry to the root, returning path in root→leaf order."""
        path: list[SessionEntry] = []
        start_id = from_id if from_id is not None else self._leaf_id
        current = self._by_id.get(start_id) if start_id else None

        while current is not None:
            path.insert(0, current)
            current = self._by_id.get(current.parent_id) if current.parent_id else None

        return path

    def build_session_context(self) -> SessionContext:
        """Build the session context (what gets sent to the LLM)."""
        return build_session_context(self.get_entries(), self._leaf_id, self._by_id)

    def get_header(self) -> SessionHeader | None:
        """Return the session header, or None."""
        return next((e for e in self._file_entries if isinstance(e, SessionHeader)), None)

    def get_entries(self) -> list[SessionEntry]:
        """Return all session entries (excludes header)."""
        return [e for e in self._file_entries if not isinstance(e, SessionHeader)]

    def get_tree(self) -> list[SessionTreeNode]:
        """Return the session as a tree structure."""
        entries = self.get_entries()
        node_map: dict[str, SessionTreeNode] = {}
        roots: list[SessionTreeNode] = []

        for entry in entries:
            label = self._labels_by_id.get(entry.id)
            node_map[entry.id] = SessionTreeNode(entry=entry, children=[], label=label)

        for entry in entries:
            node = node_map[entry.id]
            if entry.parent_id is None or entry.parent_id == entry.id:
                roots.append(node)
            else:
                parent_node = node_map.get(entry.parent_id)
                if parent_node is not None:
                    parent_node.children.append(node)
                else:
                    roots.append(node)

        # Sort children by timestamp (oldest first)
        stack = list(roots)
        while stack:
            node = stack.pop()
            node.children.sort(
                key=lambda n: (
                    datetime.fromisoformat(n.entry.timestamp.replace("Z", "+00:00")).timestamp()
                    if n.entry.timestamp
                    else 0.0
                )
            )
            stack.extend(node.children)

        return roots

    # -------------------------------------------------------------------------
    # Branching
    # -------------------------------------------------------------------------

    def branch(self, branch_from_id: str) -> None:
        """Move the leaf pointer to an earlier entry to start a new branch."""
        if branch_from_id not in self._by_id:
            raise ValueError(f"Entry {branch_from_id} not found")
        self._leaf_id = branch_from_id

    def reset_leaf(self) -> None:
        """Reset the leaf pointer to None (before any entries)."""
        self._leaf_id = None

    def branch_with_summary(
        self,
        branch_from_id: str | None,
        summary: str,
        details: Any = None,
        from_hook: bool | None = None,
    ) -> str:
        """Branch and append a branch summary entry. Returns entry ID."""
        if branch_from_id is not None and branch_from_id not in self._by_id:
            raise ValueError(f"Entry {branch_from_id} not found")

        self._leaf_id = branch_from_id

        entry = BranchSummaryEntry(
            id=_generate_id(self._by_id),
            parent_id=branch_from_id,
            timestamp=_now_iso(),
            from_id=branch_from_id if branch_from_id else "root",
            summary=summary,
            details=details,
            from_hook=from_hook,
        )
        self._append_entry(entry)
        return entry.id

    def create_branched_session(self, leaf_id: str) -> str | None:
        """Create a new session file containing only the path to the given leaf.

        Returns the new session file path, or None if not persisting.
        """
        path = self.get_branch(leaf_id)

        if not path:
            raise ValueError(f"Entry {leaf_id} not found")

        # Filter out label entries from path; re-add them fresh
        path_without_labels = [e for e in path if not isinstance(e, LabelEntry)]

        new_session_id = str(uuid.uuid4())
        timestamp = _now_iso()
        file_timestamp = timestamp.replace(":", "-").replace(".", "-")

        path_entry_ids = {e.id for e in path_without_labels}
        labels_to_write: list[tuple[str, str]] = [
            (tid, label) for tid, label in self._labels_by_id.items() if tid in path_entry_ids
        ]

        new_header = SessionHeader(
            version=CURRENT_SESSION_VERSION,
            id=new_session_id,
            timestamp=timestamp,
            cwd=self._cwd,
            parent_session=self._session_file if self._persist else None,
        )

        if self._persist:
            new_session_file = os.path.join(self.get_session_dir(), f"{file_timestamp}_{new_session_id}.jsonl")
            with open(new_session_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(_serialize_entry(new_header)) + "\n")
                for entry in path_without_labels:
                    f.write(json.dumps(_serialize_entry(entry)) + "\n")

            # Write label entries
            last_id = path_without_labels[-1].id if path_without_labels else None
            parent_id = last_id
            label_entries: list[LabelEntry] = []
            combined_ids = set(path_entry_ids)
            for target_id, label in labels_to_write:
                lentry = LabelEntry(
                    id=_generate_id(combined_ids),
                    parent_id=parent_id,
                    timestamp=_now_iso(),
                    target_id=target_id,
                    label=label,
                )
                with open(new_session_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(_serialize_entry(lentry)) + "\n")
                combined_ids.add(lentry.id)
                label_entries.append(lentry)
                parent_id = lentry.id

            self._file_entries = [new_header, *path_without_labels, *label_entries]
            self._session_id = new_session_id
            self._session_file = new_session_file
            self._flushed = True
            self._build_index()
            return new_session_file

        # In-memory mode
        label_entries = []
        parent_id = path_without_labels[-1].id if path_without_labels else None
        combined_ids = set(path_entry_ids)
        for target_id, label in labels_to_write:
            lentry = LabelEntry(
                id=_generate_id(combined_ids),
                parent_id=parent_id,
                timestamp=_now_iso(),
                target_id=target_id,
                label=label,
            )
            label_entries.append(lentry)
            combined_ids.add(lentry.id)
            parent_id = lentry.id

        self._file_entries = [new_header, *path_without_labels, *label_entries]
        self._session_id = new_session_id
        self._build_index()
        return None

    # -------------------------------------------------------------------------
    # Factory class methods
    # -------------------------------------------------------------------------

    @classmethod
    def create(cls, cwd: str, session_dir: str | None = None) -> SessionManager:
        """Create a new session manager with a fresh session."""
        resolved_dir = session_dir or _get_default_session_dir(cwd)
        return cls(cwd, resolved_dir, None, True)

    @classmethod
    def open(cls, path: str, session_dir: str | None = None) -> SessionManager:
        """Open a specific session file."""
        entries = load_entries_from_file(path)
        header = next((e for e in entries if isinstance(e, SessionHeader)), None)
        cwd = header.cwd if header else os.getcwd()
        resolved_dir = session_dir or str(Path(path).parent.resolve())
        return cls(cwd, resolved_dir, path, True)

    @classmethod
    def continue_recent(cls, cwd: str, session_dir: str | None = None) -> SessionManager:
        """Continue the most recent session, or create a new one."""
        resolved_dir = session_dir or _get_default_session_dir(cwd)
        most_recent = find_most_recent_session(resolved_dir)
        if most_recent:
            return cls(cwd, resolved_dir, most_recent, True)
        return cls(cwd, resolved_dir, None, True)

    @classmethod
    def in_memory(cls, cwd: str | None = None) -> SessionManager:
        """Create an in-memory session manager (no file persistence)."""
        return cls(cwd or os.getcwd(), "", None, False)

    @classmethod
    def fork_from(
        cls,
        source_path: str,
        target_cwd: str,
        session_dir: str | None = None,
    ) -> SessionManager:
        """Fork a session from another project directory into the current project."""
        source_entries = load_entries_from_file(source_path)
        if not source_entries:
            raise ValueError(f"Cannot fork: source session file is empty or invalid: {source_path}")

        source_header = next((e for e in source_entries if isinstance(e, SessionHeader)), None)
        if not source_header:
            raise ValueError(f"Cannot fork: source session has no header: {source_path}")

        resolved_dir = session_dir or _get_default_session_dir(target_cwd)
        os.makedirs(resolved_dir, exist_ok=True)

        new_session_id = str(uuid.uuid4())
        timestamp = _now_iso()
        file_timestamp = timestamp.replace(":", "-").replace(".", "-")
        new_session_file = os.path.join(resolved_dir, f"{file_timestamp}_{new_session_id}.jsonl")

        new_header = SessionHeader(
            version=CURRENT_SESSION_VERSION,
            id=new_session_id,
            timestamp=timestamp,
            cwd=target_cwd,
            parent_session=source_path,
        )

        with open(new_session_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(_serialize_entry(new_header)) + "\n")
            for entry in source_entries:
                if not isinstance(entry, SessionHeader):
                    f.write(json.dumps(_serialize_entry(entry)) + "\n")

        return cls(target_cwd, resolved_dir, new_session_file, True)

    @classmethod
    async def list_sessions(
        cls,
        cwd: str,
        session_dir: str | None = None,
        on_progress: Any = None,
    ) -> list[SessionInfo]:
        """List all sessions for a directory, sorted by modified time (newest first)."""

        resolved_dir = session_dir or _get_default_session_dir(cwd)
        sessions = await _list_sessions_from_dir(resolved_dir, on_progress)
        sessions.sort(key=lambda s: s.modified, reverse=True)
        return sessions

    @classmethod
    async def list_all(cls, on_progress: Any = None) -> list[SessionInfo]:
        """List all sessions across all project directories."""
        import asyncio

        agent_dir = _get_agent_dir()
        sessions_dir = os.path.join(agent_dir, "sessions")

        if not os.path.exists(sessions_dir):
            return []

        try:
            subdirs = [
                os.path.join(sessions_dir, entry)
                for entry in os.listdir(sessions_dir)
                if os.path.isdir(os.path.join(sessions_dir, entry))
            ]

            all_files: list[str] = []
            for d in subdirs:
                with contextlib.suppress(OSError):
                    all_files.extend(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl"))

            total = len(all_files)
            loaded = 0
            sessions: list[SessionInfo] = []

            async def _load(fp: str) -> SessionInfo | None:
                nonlocal loaded
                info = await _build_session_info(fp)
                loaded += 1
                if on_progress is not None:
                    on_progress(loaded, total)
                return info

            results = await asyncio.gather(*[_load(fp) for fp in all_files])
            for info in results:
                if info is not None:
                    sessions.append(info)

            sessions.sort(key=lambda s: s.modified, reverse=True)
            return sessions
        except OSError:
            return []


# ---------------------------------------------------------------------------
# Async helper functions
# ---------------------------------------------------------------------------


async def _build_session_info(file_path: str) -> SessionInfo | None:
    """Build SessionInfo from a session file (async for concurrency)."""
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _build_session_info_sync, file_path)


def _build_session_info_sync(file_path: str) -> SessionInfo | None:
    """Synchronously build SessionInfo from a session file."""
    try:
        entries = load_entries_from_file(file_path)
        if not entries:
            return None

        header = next((e for e in entries if isinstance(e, SessionHeader)), None)
        if not header:
            return None

        file_stat = os.stat(file_path)
        message_count = 0
        first_message = ""
        all_messages: list[str] = []
        name: str | None = None

        for entry in entries:
            if isinstance(entry, SessionInfoEntry) and entry.name:
                name = entry.name.strip()
            if not isinstance(entry, SessionMessageEntry):
                continue
            message_count += 1
            msg = entry.message
            role = getattr(msg, "role", "")
            if role not in ("user", "assistant"):
                continue
            content = getattr(msg, "content", "")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(getattr(b, "text", "") for b in content if hasattr(b, "text"))
            if not text:
                continue
            all_messages.append(text)
            if not first_message and role == "user":
                first_message = text

        # Compute modified time from last activity
        last_activity: float | None = None
        for entry in entries:
            if not isinstance(entry, SessionMessageEntry):
                continue
            msg = entry.message
            role = getattr(msg, "role", "")
            if role not in ("user", "assistant"):
                continue
            ts = getattr(msg, "timestamp", None)
            if isinstance(ts, (int, float)) and ts > 0:
                last_activity = max(last_activity or 0, float(ts))

        if last_activity is not None:
            modified = datetime.fromtimestamp(last_activity / 1000, tz=UTC)
        else:
            modified = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)

        created_str = header.timestamp
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            created = datetime.fromtimestamp(file_stat.st_ctime, tz=UTC)

        return SessionInfo(
            path=file_path,
            id=header.id,
            cwd=header.cwd,
            name=name,
            parent_session_path=header.parent_session,
            created=created,
            modified=modified,
            message_count=message_count,
            first_message=first_message or "(no messages)",
            all_messages_text=" ".join(all_messages),
        )
    except Exception:
        logger.warning("Failed to build session info for %s", file_path, exc_info=True)
        return None


async def _list_sessions_from_dir(
    session_dir: str,
    on_progress: Any = None,
) -> list[SessionInfo]:
    """List all sessions from a directory."""
    import asyncio

    dir_path = Path(session_dir)
    if not dir_path.exists():
        return []

    try:
        files = [str(p) for p in dir_path.glob("*.jsonl")]
    except OSError:
        return []

    total = len(files)
    loaded = 0
    sessions: list[SessionInfo] = []

    async def _load(fp: str) -> SessionInfo | None:
        nonlocal loaded
        info = await _build_session_info(fp)
        loaded += 1
        if on_progress is not None:
            on_progress(loaded, total)
        return info

    results = await asyncio.gather(*[_load(fp) for fp in files])
    for info in results:
        if info is not None:
            sessions.append(info)

    return sessions


# ---------------------------------------------------------------------------
# ReadonlySessionManager Protocol
# ---------------------------------------------------------------------------


class ReadonlySessionManager(Protocol):
    """Read-only view of a SessionManager.

    Port of ReadonlySessionManager from packages/coding-agent/src/core/session-manager.ts.
    Provides only the getter methods needed for consumers that do not modify sessions.
    """

    def get_cwd(self) -> str: ...
    def get_session_dir(self) -> str: ...
    def get_session_id(self) -> str: ...
    def get_session_file(self) -> str | None: ...
    def get_leaf_id(self) -> str | None: ...
    def get_leaf_entry(self) -> SessionEntry | None: ...
    def get_entry(self, entry_id: str) -> SessionEntry | None: ...
    def get_label(self, entry_id: str) -> str | None: ...
    def get_branch(self, from_id: str | None = None) -> list[SessionEntry]: ...
    def get_header(self) -> SessionHeader | None: ...
    def get_entries(self) -> list[SessionEntry]: ...
    def get_tree(self) -> list[SessionTreeNode]: ...
    def get_session_name(self) -> str | None: ...
