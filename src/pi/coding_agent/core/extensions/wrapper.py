"""Tool wrappers for extensions.

Port of packages/coding-agent/src/core/extensions/wrapper.ts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.coding_agent.core.extensions.runner import ExtensionRunner
from pi.coding_agent.core.extensions.types import RegisteredTool, ToolCallEvent, ToolResultEvent


class _WrappedRegisteredTool(AgentTool):
    """AgentTool wrapper around a RegisteredTool."""

    def __init__(self, registered_tool: RegisteredTool, runner: ExtensionRunner) -> None:
        self._registered_tool = registered_tool
        self._runner = runner

    @property
    def name(self) -> str:
        return self._registered_tool.definition.name

    @property
    def label(self) -> str:
        return self._registered_tool.definition.label

    @property
    def description(self) -> str:
        return self._registered_tool.definition.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._registered_tool.definition.parameters

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        ctx = self._runner.create_context()
        result = await self._registered_tool.definition.execute(tool_call_id, params, signal, on_update, ctx)
        return result  # type: ignore[no-any-return]


def wrap_registered_tool(
    registered_tool: RegisteredTool,
    runner: ExtensionRunner,
) -> AgentTool:
    """Wrap a RegisteredTool into an AgentTool.

    Uses the runner's create_context() for consistent context across tools and event handlers.
    """
    return _WrappedRegisteredTool(registered_tool, runner)


def wrap_registered_tools(
    registered_tools: list[RegisteredTool],
    runner: ExtensionRunner,
) -> list[AgentTool]:
    """Wrap all registered tools into AgentTools."""
    return [wrap_registered_tool(rt, runner) for rt in registered_tools]


class _ExtensionWrappedTool(AgentTool):
    """Tool wrapped with extension callbacks for interception."""

    def __init__(self, tool: AgentTool, runner: ExtensionRunner) -> None:
        self._tool = tool
        self._runner = runner

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def label(self) -> str:
        return self._tool.label

    @property
    def description(self) -> str:
        return self._tool.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._tool.parameters

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        # Emit tool_call event - extensions can block execution
        if self._runner.has_handlers("tool_call"):
            try:
                call_result = await self._runner.emit_tool_call(
                    ToolCallEvent(
                        type="tool_call",
                        tool_name=self._tool.name,
                        tool_call_id=tool_call_id,
                        input=params,
                    )
                )
                if call_result and call_result.block:
                    reason = call_result.reason or "Tool execution was blocked by an extension"
                    raise RuntimeError(reason)
            except RuntimeError:
                raise
            except Exception as err:
                raise RuntimeError(f"Extension failed, blocking execution: {err}") from err

        # Execute the actual tool
        try:
            result = await self._tool.execute(tool_call_id, params, signal, on_update)

            # Emit tool_result event - extensions can modify the result
            if self._runner.has_handlers("tool_result"):
                result_event = await self._runner.emit_tool_result(
                    ToolResultEvent(
                        type="tool_result",
                        tool_name=self._tool.name,
                        tool_call_id=tool_call_id,
                        input=params,
                        content=list(result.content),
                        details=result.details,
                        is_error=False,
                    )
                )
                if result_event:
                    return AgentToolResult(
                        content=result_event.content if result_event.content is not None else result.content,
                        details=result_event.details if result_event.details is not None else result.details,
                    )

            return result

        except Exception as err:
            # Emit tool_result event for errors
            if self._runner.has_handlers("tool_result"):
                await self._runner.emit_tool_result(
                    ToolResultEvent(
                        type="tool_result",
                        tool_name=self._tool.name,
                        tool_call_id=tool_call_id,
                        input=params,
                        content=[{"type": "text", "text": str(err)}],
                        details=None,
                        is_error=True,
                    )
                )
            raise


def wrap_tool_with_extensions(
    tool: AgentTool,
    runner: ExtensionRunner,
) -> AgentTool:
    """Wrap a tool with extension callbacks for interception.

    - Emits tool_call event before execution (can block)
    - Emits tool_result event after execution (can modify result)
    """
    return _ExtensionWrappedTool(tool, runner)


def wrap_tools_with_extensions(
    tools: list[AgentTool],
    runner: ExtensionRunner,
) -> list[AgentTool]:
    """Wrap all tools with extension callbacks."""
    return [wrap_tool_with_extensions(tool, runner) for tool in tools]
