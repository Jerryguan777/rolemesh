"""
RoleMesh in-process MCP server.

Defines MCP tools that publish to NATS for the host process to consume.
Uses create_sdk_mcp_server / @tool for in-process registration (no stdio).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from croniter import croniter

if TYPE_CHECKING:
    from nats.js.client import JetStreamContext


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Return an MCP tool result dict."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def create_rolemesh_mcp_server(
    chat_jid: str,
    group_folder: str,
    is_main: bool,
    js: JetStreamContext,
    job_id: str,
) -> Any:
    """Create and return an in-process MCP server with all RoleMesh tools."""

    _bg_tasks: set[asyncio.Task[None]] = set()

    def _publish(subject: str, data: dict[str, Any]) -> None:
        """Publish a JSON message to NATS JetStream (fire-and-forget)."""
        task = asyncio.ensure_future(
            js.publish(subject, json.dumps(data, indent=2).encode())  # type: ignore[arg-type]
        )
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)

    # --- send_message ---
    @tool(
        "send_message",
        "Send a message to the user or group immediately while you're still running. "
        "Use this for progress updates or to send multiple messages.",
        {"text": str, "sender": str},
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": "message",
            "chatJid": chat_jid,
            "text": args["text"],
            "groupFolder": group_folder,
            "timestamp": datetime.now().isoformat(),
        }
        if args.get("sender"):
            data["sender"] = args["sender"]
        _publish(f"agent.{job_id}.messages", data)
        return _text_result("Message sent.")

    # --- schedule_task ---
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
            "target_group_jid": str,
        },
    )
    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        prompt = args["prompt"]
        schedule_type = args["schedule_type"]
        schedule_value = args["schedule_value"]
        context_mode = args.get("context_mode", "group")
        target_group_jid = args.get("target_group_jid")

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

        target_jid = target_group_jid if is_main and target_group_jid else chat_jid
        rand_suffix = f"{time.time_ns() % 10**8:08x}"
        task_id = f"task-{int(time.time() * 1000)}-{rand_suffix}"

        _publish(
            f"agent.{job_id}.tasks",
            {
                "type": "schedule_task",
                "taskId": task_id,
                "prompt": prompt,
                "schedule_type": schedule_type,
                "schedule_value": schedule_value,
                "context_mode": context_mode or "group",
                "targetJid": target_jid,
                "createdBy": group_folder,
                "groupFolder": group_folder,
                "timestamp": datetime.now().isoformat(),
            },
        )
        return _text_result(f"Task {task_id} scheduled: {schedule_type} - {schedule_value}")

    # --- list_tasks ---
    @tool("list_tasks", "List all scheduled tasks.", {})
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        try:
            kv = await js.key_value("snapshots")
            entry = await kv.get(f"{group_folder}.tasks")
            all_tasks: list[dict[str, Any]] = json.loads(entry.value)
            tasks = all_tasks if is_main else [t for t in all_tasks if t.get("groupFolder") == group_folder]
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

    # --- pause_task ---
    @tool("pause_task", "Pause a scheduled task.", {"task_id": str})
    async def pause_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        _publish(
            f"agent.{job_id}.tasks",
            {
                "type": "pause_task",
                "taskId": task_id,
                "groupFolder": group_folder,
                "isMain": is_main,
                "timestamp": datetime.now().isoformat(),
            },
        )
        return _text_result(f"Task {task_id} pause requested.")

    # --- resume_task ---
    @tool("resume_task", "Resume a paused task.", {"task_id": str})
    async def resume_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        _publish(
            f"agent.{job_id}.tasks",
            {
                "type": "resume_task",
                "taskId": task_id,
                "groupFolder": group_folder,
                "isMain": is_main,
                "timestamp": datetime.now().isoformat(),
            },
        )
        return _text_result(f"Task {task_id} resume requested.")

    # --- cancel_task ---
    @tool("cancel_task", "Cancel and delete a scheduled task.", {"task_id": str})
    async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        _publish(
            f"agent.{job_id}.tasks",
            {
                "type": "cancel_task",
                "taskId": task_id,
                "groupFolder": group_folder,
                "isMain": is_main,
                "timestamp": datetime.now().isoformat(),
            },
        )
        return _text_result(f"Task {task_id} cancellation requested.")

    # --- update_task ---
    @tool(
        "update_task",
        "Update an existing scheduled task. Only provided fields are changed.",
        {"task_id": str, "prompt": str, "schedule_type": str, "schedule_value": str},
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = args["task_id"]
        stype = args.get("schedule_type")
        sval = args.get("schedule_value")

        if (stype == "cron" or (not stype and sval)) and sval:
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
            "groupFolder": group_folder,
            "isMain": str(is_main),
            "timestamp": datetime.now().isoformat(),
        }
        if args.get("prompt") is not None:
            data["prompt"] = args["prompt"]
        if stype is not None:
            data["schedule_type"] = stype
        if sval is not None:
            data["schedule_value"] = sval

        _publish(f"agent.{job_id}.tasks", data)
        return _text_result(f"Task {task_id} update requested.")

    # --- register_group ---
    @tool(
        "register_group",
        "Register a new chat/group so the agent can respond there. Main group only.",
        {"jid": str, "name": str, "folder": str, "trigger": str},
    )
    async def register_group(args: dict[str, Any]) -> dict[str, Any]:
        if not is_main:
            return _text_result("Only the main group can register new groups.", is_error=True)
        _publish(
            f"agent.{job_id}.tasks",
            {
                "type": "register_group",
                "jid": args["jid"],
                "name": args["name"],
                "folder": args["folder"],
                "trigger": args["trigger"],
                "groupFolder": group_folder,
                "timestamp": datetime.now().isoformat(),
            },
        )
        return _text_result(f'Group "{args["name"]}" registered.')

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
            register_group,
        ],
    )
