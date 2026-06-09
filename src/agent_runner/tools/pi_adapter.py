"""
Pi backend adapter — wraps shared tool functions as pi.agent.types.AgentTool instances.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pi.agent.types import AgentTool, AgentToolResult
from pi.ai.types import TextContent

from . import rolemesh_tools as rt

if TYPE_CHECKING:
    import asyncio

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
        content_list = result.get("content", [])
        text = content_list[0].get("text", "") if content_list else ""
        is_error = result.get("isError", False)
        if is_error:
            text = f"[Error] {text}"
        return AgentToolResult(
            content=[TextContent(text=text)],
            details={"isError": is_error} if is_error else None,
        )


_DELEGATION_TOOLS = frozenset({"delegate_to_agent", "list_agents"})
_TASK_MANAGEMENT_TOOLS = frozenset({
    "schedule_task",
    "list_tasks",
    "pause_task",
    "resume_task",
    "cancel_task",
    "update_task",
})


def create_rolemesh_tools(
    ctx: ToolContext,
    *,
    register_send_message: bool = False,
    register_delegation: bool = False,
    register_task_management: bool = False,
) -> list[AgentTool]:
    """Create all RoleMesh IPC tools as Pi AgentTool instances.

    Tools are filtered at registration so the LLM never sees options it
    cannot legitimately exercise — saving context, preventing wasted
    tool-use turns, and keeping the v1.5 sub-chip display honest.
    Runtime permission checks in ``rolemesh_tools`` remain as
    defence-in-depth.

    Flag → permission mapping (read by callers from ``init.permissions``):

      - ``register_send_message``: ``init.is_scheduled_task``. Reserved
        for background notifications; interactive turns deliver replies
        via natural assistant text.
      - ``register_delegation``: ``agent_delegate``. Gates
        ``delegate_to_agent`` and ``list_agents`` — both are
        frontdesk-only routing tools. The orchestrator-side handler in
        ``rolemesh.orchestration.delegation`` enforces the same gate as
        a second layer.
      - ``register_task_management``: ``task_schedule OR
        task_manage_others``. Gates the six task lifecycle tools
        (schedule / list / pause / resume / cancel / update). The
        permissions split exists so an agent can manage its own tasks
        (``task_schedule``) or another agent's (``task_manage_others``)
        — either alone is enough to legitimately call these tools.
    """
    tools: list[AgentTool] = []
    for defn in rt.TOOL_DEFINITIONS:
        name = defn["name"]
        if name == "send_message" and not register_send_message:
            continue
        if name in _DELEGATION_TOOLS and not register_delegation:
            continue
        if name in _TASK_MANAGEMENT_TOOLS and not register_task_management:
            continue
        fn = rt.TOOL_FUNCTIONS[name]
        tools.append(
            RoleMeshAgentTool(
                tool_name=name,
                tool_description=defn["description"],
                tool_parameters=defn["parameters"],
                fn=fn,
                ctx=ctx,
            )
        )
    return tools
