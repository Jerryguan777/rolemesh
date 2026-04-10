"""
RoleMesh IPC tools — pure business logic.

Each tool is a plain async function with signature:
    async def tool_name(args: dict, ctx: ToolContext) -> ToolResult

ToolResult follows the MCP result format:
    {"content": [{"type": "text", "text": "..."}], "isError": True|absent}

Backend adapters (claude_adapter.py, pi_adapter.py) wrap these into the
format each backend expects.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from croniter import croniter

from .context import ToolContext

# Type alias for MCP-style tool results.
ToolResult = dict[str, Any]


# -- Tool metadata for adapter generation --

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": (
            "Send a message to the user or group immediately while you're still running. "
            "Use this for progress updates or to send multiple messages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send"},
                "sender": {"type": "string", "description": "Optional sender name override"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "schedule_task",
        "description": (
            "Schedule a recurring or one-time task. Returns the task ID.\n\n"
            "CONTEXT MODE:\n"
            "\u2022 group: runs with chat history\n"
            "\u2022 isolated: fresh session\n\n"
            "SCHEDULE VALUE FORMAT (local timezone):\n"
            '\u2022 cron: "0 9 * * *"\n'
            '\u2022 interval: milliseconds like "300000"\n'
            '\u2022 once: "2026-02-01T15:30:00" (no Z suffix)'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "schedule_type": {"type": "string"},
                "schedule_value": {"type": "string"},
                "context_mode": {"type": "string"},
            },
            "required": ["prompt", "schedule_type", "schedule_value"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all scheduled tasks.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_task",
        "description": "Pause a scheduled task.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "resume_task",
        "description": "Resume a paused task.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel_task",
        "description": "Cancel and delete a scheduled task.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "update_task",
        "description": "Update an existing scheduled task. Only provided fields are changed.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "prompt": {"type": "string"},
                "schedule_type": {"type": "string"},
                "schedule_value": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
]


def _text_result(text: str, *, is_error: bool = False) -> ToolResult:
    """Return an MCP tool result dict."""
    result: ToolResult = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def send_message(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    data: dict[str, Any] = {
        "type": "message",
        "chatJid": ctx.chat_jid,
        "text": args["text"],
        "groupFolder": ctx.group_folder,
        "tenantId": ctx.tenant_id,
        "coworkerId": ctx.coworker_id,
        "timestamp": datetime.now().isoformat(),
    }
    if args.get("sender"):
        data["sender"] = args["sender"]
    ctx.publish(f"agent.{ctx.job_id}.messages", data)
    return _text_result("Message sent.")


async def schedule_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not ctx.can_schedule:
        return _text_result(
            "Permission denied: task:schedule is not enabled for this agent.",
            is_error=True,
        )

    prompt = args["prompt"]
    schedule_type = args["schedule_type"]
    schedule_value = args["schedule_value"]
    context_mode = args.get("context_mode", "group")

    if schedule_type not in ("cron", "interval", "once"):
        return _text_result(f'Invalid schedule_type: "{schedule_type}".', is_error=True)

    if schedule_type == "cron":
        try:
            croniter(schedule_value)
        except (ValueError, KeyError):
            return _text_result(f'Invalid cron: "{schedule_value}".', is_error=True)
    elif schedule_type == "interval":
        try:
            ms = int(schedule_value)
            if ms <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return _text_result(f'Invalid interval: "{schedule_value}".', is_error=True)
    elif schedule_type == "once":
        if re.search(r"[Zz]$", schedule_value) or re.search(r"[+-]\d{2}:\d{2}$", schedule_value):
            return _text_result(
                f'Timestamp must be local time without Z. Got "{schedule_value}".',
                is_error=True,
            )
        try:
            datetime.fromisoformat(schedule_value)
        except ValueError:
            return _text_result(f'Invalid timestamp: "{schedule_value}".', is_error=True)

    rand_suffix = f"{time.time_ns() % 10**8:08x}"
    task_id = f"task-{int(time.time() * 1000)}-{rand_suffix}"

    ctx.publish(
        f"agent.{ctx.job_id}.tasks",
        {
            "type": "schedule_task",
            "taskId": task_id,
            "prompt": prompt,
            "schedule_type": schedule_type,
            "schedule_value": schedule_value,
            "context_mode": context_mode or "group",
            "targetCoworkerId": ctx.coworker_id,
            "conversationId": ctx.conversation_id,
            "createdBy": ctx.group_folder,
            "groupFolder": ctx.group_folder,
            "tenantId": ctx.tenant_id,
            "coworkerId": ctx.coworker_id,
            "timestamp": datetime.now().isoformat(),
        },
    )
    return _text_result(f"Task {task_id} scheduled: {schedule_type} - {schedule_value}")


async def list_tasks(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        kv = await ctx.js.key_value("snapshots")
        key = f"{ctx.tenant_id}.{ctx.group_folder}.tasks" if ctx.tenant_id else f"{ctx.group_folder}.tasks"
        entry = await kv.get(key)
        all_tasks: list[dict[str, Any]] = json.loads(entry.value)
        tasks = (
            all_tasks
            if ctx.has_tenant_scope
            else [t for t in all_tasks if t.get("coworkerFolder") == ctx.group_folder]
        )
        if not tasks:
            return _text_result("No scheduled tasks found.")
        lines = [
            f"- [{t.get('id', '?')}] {t.get('prompt', '')[:50]}... "
            f"({t.get('schedule_type', '?')}: {t.get('schedule_value', '?')}) - "
            f"{t.get('status', '?')}, next: {t.get('next_run', 'N/A')}"
            for t in tasks
        ]
        return _text_result("Scheduled tasks:\n" + "\n".join(lines))
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return _text_result(f"Error reading tasks: {exc}")


async def pause_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = args["task_id"]
    ctx.publish(
        f"agent.{ctx.job_id}.tasks",
        {
            "type": "pause_task",
            "taskId": task_id,
            "groupFolder": ctx.group_folder,
            "tenantId": ctx.tenant_id,
            "coworkerId": ctx.coworker_id,
            "timestamp": datetime.now().isoformat(),
        },
    )
    return _text_result(f"Task {task_id} pause requested.")


async def resume_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = args["task_id"]
    ctx.publish(
        f"agent.{ctx.job_id}.tasks",
        {
            "type": "resume_task",
            "taskId": task_id,
            "groupFolder": ctx.group_folder,
            "tenantId": ctx.tenant_id,
            "coworkerId": ctx.coworker_id,
            "timestamp": datetime.now().isoformat(),
        },
    )
    return _text_result(f"Task {task_id} resume requested.")


async def cancel_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = args["task_id"]
    ctx.publish(
        f"agent.{ctx.job_id}.tasks",
        {
            "type": "cancel_task",
            "taskId": task_id,
            "groupFolder": ctx.group_folder,
            "tenantId": ctx.tenant_id,
            "coworkerId": ctx.coworker_id,
            "timestamp": datetime.now().isoformat(),
        },
    )
    return _text_result(f"Task {task_id} cancellation requested.")


async def update_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task_id = args["task_id"]
    stype = args.get("schedule_type")
    sval = args.get("schedule_value")

    if stype == "cron" and sval:
        try:
            croniter(sval)
        except (ValueError, KeyError):
            return _text_result(f'Invalid cron: "{sval}".', is_error=True)
    if stype == "interval" and sval:
        try:
            ms = int(sval)
            if ms <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return _text_result(f'Invalid interval: "{sval}".', is_error=True)

    data: dict[str, Any] = {
        "type": "update_task",
        "taskId": task_id,
        "groupFolder": ctx.group_folder,
        "tenantId": ctx.tenant_id,
        "coworkerId": ctx.coworker_id,
        "timestamp": datetime.now().isoformat(),
    }
    if args.get("prompt") is not None:
        data["prompt"] = args["prompt"]
    if stype is not None:
        data["schedule_type"] = stype
    if sval is not None:
        data["schedule_value"] = sval

    ctx.publish(f"agent.{ctx.job_id}.tasks", data)
    return _text_result(f"Task {task_id} update requested.")


# Map tool names to their implementation functions.
TOOL_FUNCTIONS: dict[str, Any] = {
    "send_message": send_message,
    "schedule_task": schedule_task,
    "list_tasks": list_tasks,
    "pause_task": pause_task,
    "resume_task": resume_task,
    "cancel_task": cancel_task,
    "update_task": update_task,
}
