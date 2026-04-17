"""
Claude SDK backend — wraps claude_agent_sdk as an AgentBackend.

Extracted from the original main.py. All Claude-specific logic lives here;
the NATS bridge in main.py is backend-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ToolUseBlock, query

from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

from .backend import (
    BackendEvent,
    CompactionEvent,
    ErrorEvent,
    ResultEvent,
    RunningEvent,
    SessionInitEvent,
    ToolUseEvent,
    tool_input_preview,
)
from .message_stream import MessageStream
from .tools.claude_adapter import create_rolemesh_mcp_server
from .tools.context import ToolContext


def _log(message: str) -> None:
    print(f"[claude-backend] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Transcript archiving helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(summary: str) -> str:
    import re

    name = summary.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    return name[:50]


def _generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def _parse_transcript(content: str) -> list[tuple[str, str]]:
    """Parse JSONL transcript into (role, text) pairs."""
    messages: list[tuple[str, str]] = []
    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                msg_content = entry["message"]["content"]
                text = msg_content if isinstance(msg_content, str) else "".join(c.get("text", "") for c in msg_content)
                if text:
                    messages.append(("user", text))
            elif entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                text_parts = [c.get("text", "") for c in entry["message"]["content"] if c.get("type") == "text"]
                text = "".join(text_parts)
                if text:
                    messages.append(("assistant", text))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    return messages


def _get_session_summary(session_id: str, transcript_path: str) -> str | None:
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"
    if not index_path.exists():
        return None
    try:
        index_data = json.loads(index_path.read_text())
        for entry in index_data.get("entries", []):
            if entry.get("sessionId") == session_id:
                return entry.get("summary")
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError):
        pass
    return None


def _create_pre_compact_hook(assistant_name: str | None = None) -> Any:
    """Return a PreCompact hook callback that archives transcripts."""

    async def hook(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        transcript_path: str | None = getattr(input_data, "transcript_path", None)
        session_id: str | None = getattr(input_data, "session_id", None)

        if not transcript_path or not Path(transcript_path).exists():
            _log("No transcript found for archiving")
            return {}

        try:
            content = Path(transcript_path).read_text()
            messages = _parse_transcript(content)
            if not messages:
                _log("No messages to archive")
                return {}

            summary = _get_session_summary(session_id, transcript_path) if session_id else None
            name = _sanitize_filename(summary) if summary else _generate_fallback_name()

            conversations_dir = Path("/workspace/group/conversations")
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = datetime.now().strftime("%Y-%m-%d")
            filename = f"{date}-{name}.md"
            filepath = conversations_dir / filename

            now = datetime.now()
            date_str = now.strftime("%b %-d, %-I:%M %p")
            lines: list[str] = [f"# {summary or 'Conversation'}", "", f"Archived: {date_str}", "", "---", ""]
            for role, text in messages:
                sender = "User" if role == "user" else (assistant_name or "Assistant")
                content_str = text[:2000] + "..." if len(text) > 2000 else text
                lines.append(f"**{sender}**: {content_str}")
                lines.append("")
            filepath.write_text("\n".join(lines))
            _log(f"Archived conversation to {filepath}")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
            _log(f"Failed to archive transcript: {exc}")

        return {}

    return hook


# ---------------------------------------------------------------------------
# ClaudeBackend
# ---------------------------------------------------------------------------


class ClaudeBackend:
    """AgentBackend implementation wrapping claude_agent_sdk."""

    def __init__(self) -> None:
        self._listener: Callable[[BackendEvent], Awaitable[None]] | None = None
        self._session_id: str | None = None
        self._last_assistant_uuid: str | None = None
        self._stream: MessageStream | None = None
        self._close_received: asyncio.Event = asyncio.Event()
        self._sdk_env: dict[str, str | None] = dict(os.environ)

        # Claude-specific state
        self._mcp_server: Any = None
        self._init: AgentInitData | None = None
        self._assistant_name: str | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def subscribe(self, listener: Any) -> None:
        self._listener = listener

    async def _emit(self, event: BackendEvent) -> None:
        if self._listener:
            await self._listener(event)

    async def start(
        self,
        init: AgentInitData,
        tool_ctx: ToolContext,
        mcp_servers: list[McpServerSpec] | None = None,
    ) -> None:
        self._init = init
        self._session_id = init.session_id
        self._assistant_name = init.assistant_name
        self._mcp_server = create_rolemesh_mcp_server(tool_ctx)

    async def run_prompt(self, text: str) -> None:
        """Run a single query through the Claude SDK."""
        assert self._init is not None

        # Defensive per-turn signal: the SDK's SystemMessage(init) also
        # triggers RunningEvent below, but guaranteeing one here means the
        # UI status bar doesn't depend on SDK internals staying stable.
        await self._emit(RunningEvent())

        stream = MessageStream()
        stream.push(text)
        self._stream = stream

        init = self._init

        # Load global CLAUDE.md
        global_claude_md_path = Path("/workspace/global/CLAUDE.md")
        global_claude_md: str | None = None
        if init.permissions.get("data_scope") != "tenant" and global_claude_md_path.exists():
            global_claude_md = global_claude_md_path.read_text()

        # Discover extra directories
        extra_dirs: list[str] = []
        extra_base = Path("/workspace/extra")
        if extra_base.exists():
            for entry in extra_base.iterdir():
                if entry.is_dir():
                    extra_dirs.append(str(entry))

        # Build system prompt
        system_prompt: dict[str, Any] | None = None
        append_parts: list[str] = []
        if init.system_prompt:
            append_parts.append(init.system_prompt)
        if global_claude_md:
            append_parts.append(global_claude_md)
        if append_parts:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": "\n\n".join(append_parts),
            }

        # Build extra_args for resume-session-at
        extra_args: dict[str, str] | None = None
        if self._last_assistant_uuid:
            extra_args = {"resume-session-at": self._last_assistant_uuid}

        # Build MCP servers dict
        mcp_servers_dict: dict[str, Any] = {"rolemesh": self._mcp_server}

        # Build allowed tools list
        allowed_tools = [
            "Bash", "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch", "Task", "TaskOutput", "TaskStop",
            "TeamCreate", "TeamDelete", "SendMessage", "TodoWrite",
            "ToolSearch", "Skill", "NotebookEdit",
            "mcp__rolemesh__*",
        ]

        # Register external MCP servers
        if init.mcp_servers:
            for spec in init.mcp_servers:
                server_config: dict[str, Any] = {"type": spec.type, "url": spec.url}
                headers: dict[str, str] = {}
                if init.user_id:
                    headers["X-RoleMesh-User-Id"] = init.user_id
                if headers:
                    server_config["headers"] = headers
                mcp_servers_dict[spec.name] = server_config
                allowed_tools.append(f"mcp__{spec.name}__*")
                _log(f"External MCP server registered: {spec.name} ({spec.type}) -> {spec.url}")

        options = ClaudeAgentOptions(
            cwd="/workspace/group",
            add_dirs=extra_dirs if extra_dirs else None,
            resume=self._session_id,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            env=self._sdk_env,
            permission_mode="bypassPermissions",
            mcp_servers=mcp_servers_dict,
            hooks={
                "PreCompact": [HookMatcher(hooks=[_create_pre_compact_hook(self._assistant_name)])],
            },
            setting_sources=["project", "user"],
        )
        if extra_args:
            options.extra_args = extra_args

        message_count = 0
        result_count = 0

        try:
            async for message in query(prompt=stream, options=options):
                message_count += 1
                cls_name = type(message).__name__

                if cls_name == "SystemMessage":
                    subtype = getattr(message, "subtype", "")
                    data = getattr(message, "data", {})
                    if subtype == "init":
                        sid = data.get("session_id") if isinstance(data, dict) else None
                        if sid:
                            self._session_id = sid
                            await self._emit(SessionInitEvent(session_id=sid))
                            await self._emit(RunningEvent())
                            _log(f"Session initialized: {sid}")
                    elif subtype == "task_notification":
                        _log(f"Task notification: {data}" if not isinstance(data, dict) else
                             f"Task notification: task={data.get('task_id')} status={data.get('status')}")

                elif cls_name == "AssistantMessage":
                    uuid = getattr(message, "uuid", None)
                    if uuid:
                        self._last_assistant_uuid = uuid
                    # Emit one ToolUseEvent per ToolUseBlock in this message.
                    # Multiple tools in a single AssistantMessage mean parallel
                    # tool calls — emitting one event per block keeps the UI's
                    # overwrite-style progress bar showing advancement.
                    content = getattr(message, "content", None)
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, ToolUseBlock):
                                tool_name = str(block.name or "")
                                tool_input = block.input if isinstance(block.input, dict) else {}
                                await self._emit(
                                    ToolUseEvent(
                                        tool=tool_name,
                                        input_preview=tool_input_preview(tool_name, tool_input),
                                    )
                                )

                elif cls_name == "ResultMessage":
                    result_count += 1
                    text_result = getattr(message, "result", None)
                    session_id_from_result = getattr(message, "session_id", None)
                    if session_id_from_result:
                        self._session_id = session_id_from_result
                    await self._emit(ResultEvent(
                        text=text_result or None,
                        new_session_id=self._session_id,
                    ))
        except Exception as exc:
            await self._emit(ErrorEvent(error=str(exc)))
            raise

        _log(f"Query done. Messages: {message_count}, results: {result_count}")

    async def handle_follow_up(self, text: str) -> None:
        """Pipe a follow-up message into the active query stream."""
        if self._stream:
            _log(f"Piping follow-up message into active query ({len(text)} chars)")
            self._stream.push(text)

    async def abort(self) -> None:
        """End the message stream to signal the SDK to stop."""
        if self._stream:
            self._stream.end()

    async def shutdown(self) -> None:
        pass
