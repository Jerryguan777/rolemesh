"""
MCP tool bridge — discovers tools from external MCP servers and wraps them
as Pi AgentTool instances.

Tool names follow the convention: mcp__{server_name}__{tool_name}
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult
from pi.ai.types import TextContent

from .client import McpServerConnection

logger = logging.getLogger(__name__)


class McpProxiedTool(AgentTool):
    """Wraps a remote MCP tool as a Pi AgentTool."""

    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict[str, Any],
        connection: McpServerConnection,
        remote_tool_name: str,
    ) -> None:
        self._name = tool_name
        self._description = tool_description
        self._parameters = tool_parameters
        self._connection = connection
        self._remote_tool_name = remote_tool_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def label(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: Any | None = None,
    ) -> AgentToolResult:
        try:
            result_text = await self._connection.call_tool(self._remote_tool_name, params)
        except Exception as exc:
            result_text = f"MCP tool error: {exc}"
        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details=None,
        )


async def _connect_one(
    spec: Any, user_id: str
) -> tuple[list[AgentTool], McpServerConnection | None]:
    """Connect to a single MCP server and discover its tools.

    Runs in its own asyncio.Task so any anyio cancel-scope unwind from
    mcp/streamable-http internals stays contained within this task and
    does not propagate to the caller as CancelledError.

    Returns ([], None) on any failure — caller skips dead servers.
    """
    headers: dict[str, str] = {}
    if user_id:
        headers["X-RoleMesh-User-Id"] = user_id

    conn = McpServerConnection(
        name=spec.name,
        server_type=spec.type,
        url=spec.url,
        headers=headers or None,
    )
    try:
        await conn.connect()
        remote_tools = await conn.list_tools()
        logger.info("MCP server '%s': %d tools discovered", spec.name, len(remote_tools))
        tools: list[AgentTool] = [
            McpProxiedTool(
                tool_name=f"mcp__{spec.name}__{tool_info['name']}",
                tool_description=tool_info["description"],
                tool_parameters=tool_info["inputSchema"],
                connection=conn,
                remote_tool_name=tool_info["name"],
            )
            for tool_info in remote_tools
        ]
        return tools, conn
    # Catch CancelledError explicitly: mcp library uses anyio TaskGroup and
    # AsyncExitStack-based teardown can surface failures as CancelledError
    # rather than the original network/HTTP error. Without this, one dead
    # MCP server tears down the entire agent session.
    except (Exception, asyncio.CancelledError) as exc:
        logger.warning("MCP server '%s' unavailable, skipping: %s", spec.name, exc)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await conn.close()
        return [], None


async def load_mcp_tools(
    specs: list[Any],
    user_id: str = "",
) -> tuple[list[AgentTool], list[McpServerConnection]]:
    """Connect to MCP servers and discover their tools.

    Connects to all servers in parallel and isolates failures: a single
    dead server produces a warning and does not prevent other servers
    (or the agent session itself) from starting.

    Args:
        specs: List of objects with name, type, url attributes.
        user_id: User ID for credential proxy header injection.

    Returns:
        Tuple of (tools, connections). Connections must be closed on shutdown.
    """
    results = await asyncio.gather(
        *(_connect_one(spec, user_id) for spec in specs),
        return_exceptions=False,
    )

    all_tools: list[AgentTool] = []
    connections: list[McpServerConnection] = []
    for tools, conn in results:
        all_tools.extend(tools)
        if conn is not None:
            connections.append(conn)

    return all_tools, connections
