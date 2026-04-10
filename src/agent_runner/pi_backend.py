"""
Pi backend — wraps pi.coding_agent as an AgentBackend.

Uses Pi's AgentSession for multi-turn conversations with session persistence,
auto-compaction, and tool execution. Translates Pi events into BackendEvents
for the NATS bridge.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pi.agent.types import (
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    TurnEndEvent,
)
from pi.ai.types import TextContent
from pi.coding_agent.core.agent_session import AgentSession, AgentSessionEvent
from pi.coding_agent.core.sdk import CreateAgentSessionOptions, create_agent_session
from pi.coding_agent.core.resource_loader import DefaultResourceLoader, DefaultResourceLoaderOptions
from pi.coding_agent.core.session_manager import SessionManager

from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

from .backend import (
    BackendEvent,
    ErrorEvent,
    ResultEvent,
    SessionInitEvent,
)
from .mcp_client import McpServerConnection
from .tools.context import ToolContext
from .tools.mcp_loader import load_mcp_tools
from .tools.pi_adapter import create_rolemesh_tools


def _log(message: str) -> None:
    print(f"[pi-backend] {message}", file=sys.stderr, flush=True)


def _extract_text(message: Any) -> str:
    """Extract text content from a Pi AssistantMessage."""
    if not hasattr(message, "content"):
        return ""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif hasattr(block, "text"):
            parts.append(block.text)
    return "".join(parts)


def _task_done_callback(task: asyncio.Task[None]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log(f"Background task error: {exc}")


class PiBackend:
    """AgentBackend implementation wrapping Pi's AgentSession."""

    def __init__(self) -> None:
        self._listener: Callable[[BackendEvent], Awaitable[None]] | None = None
        self._session: AgentSession | None = None
        self._session_file: str | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._mcp_connections: list[McpServerConnection] = []
        self._bg_tasks: set[asyncio.Task[None]] = set()

    @property
    def session_id(self) -> str | None:
        return self._session_file

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
        cwd = "/workspace/group"

        # Session file path
        sessions_dir = Path("/workspace/sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_file = str(sessions_dir / f"{init.conversation_id}.jsonl")
        self._session_file = session_file

        # Create or open session manager
        if init.session_id and Path(init.session_id).exists():
            _log(f"Opening existing session: {init.session_id}")
            session_manager = SessionManager.open(init.session_id, cwd)
            self._session_file = init.session_id
        elif Path(session_file).exists():
            _log(f"Opening existing session file: {session_file}")
            session_manager = SessionManager.open(session_file, cwd)
        else:
            _log(f"Creating new session: {session_file}")
            session_manager = SessionManager.create(cwd)
            session_manager.set_session_file(session_file)

        # Build system prompt from coworker config + global CLAUDE.md
        custom_system_prompt: str | None = None
        append_system_prompt: str | None = None

        if init.system_prompt:
            append_system_prompt = init.system_prompt

        global_claude_md = Path("/workspace/global/CLAUDE.md")
        if init.permissions.get("data_scope") != "tenant" and global_claude_md.exists():
            md_content = global_claude_md.read_text()
            if append_system_prompt:
                append_system_prompt += "\n\n" + md_content
            else:
                append_system_prompt = md_content

        # Build resource loader with system prompt injection
        resource_loader = DefaultResourceLoader(
            DefaultResourceLoaderOptions(
                cwd=cwd,
                agent_dir=str(Path.home() / ".pi" / "agent"),
                system_prompt=custom_system_prompt,
                append_system_prompt=append_system_prompt,
            )
        )
        await resource_loader.reload()

        # Build custom tools (RoleMesh IPC tools + external MCP tools)
        custom_tools = create_rolemesh_tools(tool_ctx)

        if mcp_servers:
            mcp_tools, self._mcp_connections = await load_mcp_tools(mcp_servers)
            custom_tools.extend(mcp_tools)
            if mcp_tools:
                _log(f"Loaded {len(mcp_tools)} external MCP tools from {len(self._mcp_connections)} servers")

        # Create agent session
        result = await create_agent_session(
            CreateAgentSessionOptions(
                cwd=cwd,
                session_manager=session_manager,
                resource_loader=resource_loader,
                custom_tools=custom_tools,
            )
        )
        self._session = result.session

        # Subscribe to session events
        self._unsubscribe = self._session.subscribe(self._handle_event)

        await self._emit(SessionInitEvent(session_id=self._session_file or ""))
        _log(f"Pi session started (session_id={self._session.session_id})")

    def _schedule_emit(self, event: BackendEvent) -> None:
        """Schedule an async _emit call from a synchronous context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._emit(event))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(_task_done_callback)

    def _handle_event(self, event: AgentSessionEvent) -> None:
        """Synchronous event handler — schedules async emission."""
        if isinstance(event, TurnEndEvent):
            text = _extract_text(event.message) if hasattr(event, "message") else ""
            self._schedule_emit(ResultEvent(
                text=text or None,
                new_session_id=self._session_file,
            ))
        elif isinstance(event, AgentEndEvent):
            self._schedule_emit(ResultEvent(
                text=None,
                new_session_id=self._session_file,
            ))

    async def run_prompt(self, text: str) -> None:
        assert self._session is not None
        try:
            # AgentSession.prompt() awaits Agent.prompt() which awaits _run_loop(),
            # so this call is fully blocking until the agent finishes.
            await self._session.prompt(text)
        except Exception as exc:
            await self._emit(ErrorEvent(error=str(exc)))
            raise

    async def handle_follow_up(self, text: str) -> None:
        assert self._session is not None
        try:
            if self._session.is_streaming:
                await self._session.prompt(text, streaming_behavior="followUp")
            else:
                await self._session.prompt(text)
        except Exception as exc:
            _log(f"Follow-up error: {exc}")

    async def abort(self) -> None:
        if self._session is not None:
            await self._session.abort()

    async def shutdown(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
        if self._session is not None:
            self._session.dispose()
        for conn in self._mcp_connections:
            await conn.close()
        self._mcp_connections.clear()
