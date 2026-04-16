"""
Pi backend — wraps pi.coding_agent as an AgentBackend.

Uses Pi's AgentSession for multi-turn conversations with session persistence,
auto-compaction, and tool execution. Translates Pi events into BackendEvents
for the NATS bridge.
"""

from __future__ import annotations

import asyncio
import os
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
from pi.mcp import McpServerConnection, load_mcp_tools

from .tools.context import ToolContext
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
        self._last_result_text: str | None = None

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

        # Register LLM providers (Anthropic, OpenAI, Google, etc.)
        # Must be called before any LLM streaming; the registry starts empty.
        from pi.ai.providers.register_builtins import register_built_in_api_providers
        register_built_in_api_providers()

        # Map credential proxy env vars to Pi's expected names.
        # Container has CLAUDE_CODE_OAUTH_TOKEN (Claude Code convention);
        # Pi reads ANTHROPIC_OAUTH_TOKEN or ANTHROPIC_API_KEY.
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_OAUTH_TOKEN"):
            oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if oauth_token:
                os.environ["ANTHROPIC_OAUTH_TOKEN"] = oauth_token

        # ANTHROPIC_BASE_URL points to credential proxy (legacy path /).
        # For the /proxy/anthropic path, Pi's Anthropic SDK reads the env var.
        # OPENAI_BASE_URL points to /proxy/openai — OpenAI SDK reads it.

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
            mcp_tools, self._mcp_connections = await load_mcp_tools(
                mcp_servers, user_id=init.user_id,
            )
            custom_tools.extend(mcp_tools)
            if mcp_tools:
                _log(f"Loaded {len(mcp_tools)} external MCP tools from {len(self._mcp_connections)} servers")

        # Resolve model from PI_MODEL_ID env var.
        # Format: "model-id" (searches all providers) or "provider/model-id".
        model = None
        model_id = os.environ.get("PI_MODEL_ID")
        if model_id:
            from pi.ai.models import get_model, get_providers

            if "/" in model_id:
                provider, mid = model_id.split("/", 1)
                model = get_model(provider, mid)
            else:
                for provider in get_providers():
                    model = get_model(provider, model_id)
                    if model:
                        break

            if model:
                # Override model.base_url to route through credential proxy.
                # Pi models have hardcoded base_urls (e.g. "https://api.openai.com/v1")
                # which bypass the proxy. We replace them with the proxy URL from env vars.
                _PROXY_ENV_MAP = {
                    "openai": "OPENAI_BASE_URL",
                    "anthropic": "ANTHROPIC_BASE_URL",
                }
                proxy_env = _PROXY_ENV_MAP.get(model.provider)
                if proxy_env:
                    proxy_url = os.environ.get(proxy_env)
                    if proxy_url:
                        model.base_url = proxy_url
                        _log(f"Routing {model.provider} through proxy: {proxy_url}")
                _log(f"Using model: {model.provider}/{model.id}")
            else:
                _log(f"Warning: model '{model_id}' not found in any provider")

        # Create agent session
        result = await create_agent_session(
            CreateAgentSessionOptions(
                cwd=cwd,
                model=model,
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
        """Synchronous event handler — collects the last assistant text.

        We do NOT emit ResultEvent on every TurnEndEvent because the host
        calls notify_idle on each result, which can cause scheduling issues.
        Instead, we track the last assistant message and emit a single
        ResultEvent after run_prompt() returns.
        """
        if isinstance(event, TurnEndEvent):
            text = _extract_text(event.message) if hasattr(event, "message") else ""
            if text:
                self._last_result_text = text

    async def run_prompt(self, text: str) -> None:
        assert self._session is not None
        self._last_result_text = None
        try:
            # AgentSession.prompt() awaits Agent.prompt() which awaits _run_loop(),
            # so this call is fully blocking until the agent finishes.
            await self._session.prompt(text)
        except Exception as exc:
            await self._emit(ErrorEvent(error=str(exc)))
            raise

        # Emit a single final result after the agent is done.
        await self._emit(ResultEvent(
            text=self._last_result_text,
            new_session_id=self._session_file,
        ))

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
