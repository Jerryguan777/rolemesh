"""
Claude SDK adapter — wraps shared tool functions as an in-process MCP server.

Replaces the old ipc_mcp.py: same @tool decorators and create_sdk_mcp_server
call, but the business logic lives in rolemesh_tools.py.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import rolemesh_tools as rt
from .context import ToolContext


def create_rolemesh_mcp_server(ctx: ToolContext) -> Any:
    """Create an in-process MCP server with all RoleMesh tools for Claude SDK."""

    @tool(
        "send_message",
        # Description must stay in sync with rolemesh_tools.TOOL_DEFINITIONS.
        # The tool is designed for scheduled-task notifications only;
        # using it for interactive replies causes the reply to be dropped
        # by the orchestrator (the natural assistant text is the reply).
        "Scheduled-task notification output. Emits a message to the "
        "current conversation from a background/cron task. "
        "\n\n"
        "DO NOT call this during interactive conversations — your normal "
        "assistant text is automatically delivered to the user as the "
        "reply. Using this tool to deliver a reply will cause the reply "
        "to be dropped. "
        "\n\n"
        "Only call this when running as a scheduled task (i.e. the initial "
        "prompt starts with '[SCHEDULED TASK - ...]'), and only for the "
        "final task result you want posted to the group.",
        {"text": str, "sender": str},
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.send_message(args, ctx)

    @tool(
        "schedule_task",
        "Schedule a recurring or one-time task. Returns the task ID.\n\n"
        "CONTEXT MODE:\n"
        "\u2022 group: runs with chat history\n"
        "\u2022 isolated: fresh session\n\n"
        "SCHEDULE VALUE FORMAT (local timezone):\n"
        '\u2022 cron: "0 9 * * *"\n'
        '\u2022 interval: milliseconds like "300000"\n'
        '\u2022 once: "2026-02-01T15:30:00" (no Z suffix)',
        {
            "prompt": str,
            "schedule_type": str,
            "schedule_value": str,
            "context_mode": str,
        },
    )
    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.schedule_task(args, ctx)

    @tool("list_tasks", "List all scheduled tasks.", {})
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.list_tasks(args, ctx)

    @tool("pause_task", "Pause a scheduled task.", {"task_id": str})
    async def pause_task(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.pause_task(args, ctx)

    @tool("resume_task", "Resume a paused task.", {"task_id": str})
    async def resume_task(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.resume_task(args, ctx)

    @tool("cancel_task", "Cancel and delete a scheduled task.", {"task_id": str})
    async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.cancel_task(args, ctx)

    @tool(
        "update_task",
        "Update an existing scheduled task. Only provided fields are changed.",
        {"task_id": str, "prompt": str, "schedule_type": str, "schedule_value": str},
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        return await rt.update_task(args, ctx)

    return create_sdk_mcp_server(
        "rolemesh",
        tools=[
            send_message,
            schedule_task,
            list_tasks,
            pause_task,
            resume_task,
            cancel_task,
            update_task,
        ],
    )
