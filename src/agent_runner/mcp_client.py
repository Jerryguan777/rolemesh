"""
MCP client — connects to remote MCP servers and exposes their tools.

Used by the Pi backend to consume external MCP servers. Claude SDK has
its own MCP client built in, so this module is only needed for Pi.

Supports SSE and streamable-HTTP transports (matching McpServerSpec.type).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

# Timeout for connect + initialize handshake (seconds).
CONNECT_TIMEOUT = 30
# Timeout for a single tool call (seconds).
CALL_TOOL_TIMEOUT = 300


def _log(message: str) -> None:
    print(f"[mcp-client] {message}", file=sys.stderr, flush=True)


class McpServerConnection:
    """Manages the lifecycle of a single MCP server connection."""

    def __init__(self, name: str, server_type: str, url: str) -> None:
        self.name = name
        self.server_type = server_type
        self.url = url
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None

    async def connect(self) -> None:
        """Establish connection to the MCP server."""
        self._exit_stack = AsyncExitStack()

        try:
            if self.server_type == "sse":
                transport = sse_client(self.url)
            else:
                transport = streamablehttp_client(self.url)

            streams = await asyncio.wait_for(
                self._exit_stack.enter_async_context(transport),
                timeout=CONNECT_TIMEOUT,
            )
            # Both transports yield (read_stream, write_stream, ...) — unpack first two
            read_stream, write_stream = streams[0], streams[1]

            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(
                self._session.initialize(),
                timeout=CONNECT_TIMEOUT,
            )
            _log(f"Connected to MCP server '{self.name}' at {self.url}")
        except Exception:
            # Clean up partial resources on failure
            await self.close()
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools from the MCP server.

        Returns a list of dicts with keys: name, description, inputSchema.
        """
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.name}' not connected")

        result = await asyncio.wait_for(
            self._session.list_tools(),
            timeout=CONNECT_TIMEOUT,
        )
        tools: list[dict[str, Any]] = []
        for tool in result.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            })
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on the MCP server and return the text result."""
        if self._session is None:
            raise RuntimeError(f"MCP server '{self.name}' not connected")

        result = await asyncio.wait_for(
            self._session.call_tool(tool_name, arguments),
            timeout=CALL_TOOL_TIMEOUT,
        )

        # Collect text content from result
        parts: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(f"[binary data: {getattr(block, 'mimeType', 'unknown')}]")

        text = "\n".join(parts) if parts else ""

        if result.isError:
            return f"Error: {text}"
        return text

    async def close(self) -> None:
        """Close the connection."""
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                _log(f"Error closing MCP server '{self.name}': {exc}")
            self._exit_stack = None
            self._session = None
