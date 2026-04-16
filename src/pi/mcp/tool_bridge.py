"""
MCP tool bridge — discovers tools from external MCP servers and wraps them
as Pi AgentTool instances.

Tool names follow the convention: mcp__{server_name}__{tool_name}
"""

from __future__ import annotations

import asyncio
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


async def load_mcp_tools(
    specs: list[Any],
) -> tuple[list[AgentTool], list[McpServerConnection]]:
    """Connect to MCP servers and discover their tools.

    Args:
        specs: List of objects with name, type, url attributes.

    Returns:
        Tuple of (tools, connections). Connections must be closed on shutdown.
        Failed servers are skipped with a warning.
    """
    all_tools: list[AgentTool] = []
    connections: list[McpServerConnection] = []

    for spec in specs:
        conn = McpServerConnection(
            name=spec.name,
            server_type=spec.type,
            url=spec.url,
        )
        try:
            await conn.connect()
            connections.append(conn)

            remote_tools = await conn.list_tools()
            logger.info("MCP server '%s': %d tools discovered", spec.name, len(remote_tools))

            for tool_info in remote_tools:
                prefixed_name = f"mcp__{spec.name}__{tool_info['name']}"
                all_tools.append(
                    McpProxiedTool(
                        tool_name=prefixed_name,
                        tool_description=tool_info["description"],
                        tool_parameters=tool_info["inputSchema"],
                        connection=conn,
                        remote_tool_name=tool_info["name"],
                    )
                )
        except Exception as exc:
            logger.warning("Failed to connect to MCP server '%s': %s", spec.name, exc)
            continue

    return all_tools, connections
