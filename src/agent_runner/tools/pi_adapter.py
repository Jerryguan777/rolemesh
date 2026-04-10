"""
Pi backend adapter — wraps shared tool functions as pi.agent.types.AgentTool instances.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult
from pi.ai.types import TextContent

from . import rolemesh_tools as rt
from .context import ToolContext


class RoleMeshAgentTool(AgentTool):
    """Wraps a shared RoleMesh tool function as a Pi AgentTool."""

    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        tool_parameters: dict[str, Any],
        fn: Any,
        ctx: ToolContext,
    ) -> None:
        self._name = tool_name
        self._description = tool_description
        self._parameters = tool_parameters
        self._fn = fn
        self._ctx = ctx

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
        result = await self._fn(params, self._ctx)
        text = result.get("content", [{}])[0].get("text", "")
        is_error = result.get("isError", False)
        if is_error:
            text = f"[Error] {text}"
        return AgentToolResult(
            content=[TextContent(text=text)],
            details={"isError": is_error} if is_error else None,
        )


def create_rolemesh_tools(ctx: ToolContext) -> list[AgentTool]:
    """Create all RoleMesh IPC tools as Pi AgentTool instances."""
    tools: list[AgentTool] = []
    for defn in rt.TOOL_DEFINITIONS:
        fn = rt.TOOL_FUNCTIONS[defn["name"]]
        tools.append(
            RoleMeshAgentTool(
                tool_name=defn["name"],
                tool_description=defn["description"],
                tool_parameters=defn["parameters"],
                fn=fn,
                ctx=ctx,
            )
        )
    return tools
