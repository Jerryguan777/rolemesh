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
from typing import TYPE_CHECKING, Any

from croniter import croniter

if TYPE_CHECKING:
    from .context import ToolContext

# Type alias for MCP-style tool results.
ToolResult = dict[str, Any]


# -- Tool metadata for adapter generation --

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": (
            "Scheduled-task notification output. Emits a message to the "
            "current conversation from a background/cron task. "
            "\n\n"
            "DO NOT call this during interactive conversations — your "
            "normal assistant text is automatically delivered to the user "
            "as the reply. Using this tool to deliver a reply will cause "
            "the reply to be dropped. "
            "\n\n"
            "Only call this when running as a scheduled task (i.e. the "
            "initial prompt starts with '[SCHEDULED TASK - ...]'), and "
            "only for the final task result you want posted to the group."
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
    {
        "name": "delegate_to_agent",
        "description": (
            "Delegate the user's request to a domain specialist and return "
            "their answer.\n\n"
            "RULES:\n"
            "- Identify target by its agent id (e.g. 'trading'). Not a path.\n"
            "- Write a self-contained prompt; the target cannot see this "
            "conversation.\n"
            "- Use 'isolated' for one-shot questions; 'sticky' for a "
            "multi-turn workflow with the same specialist.\n"
            "- You may call this multiple times per turn, including in "
            "parallel.\n"
            "- If isError=true, your reply MUST quote the literal reason. "
            "See system prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "prompt": {"type": "string"},
                "context_mode": {
                    "type": "string",
                    "enum": ["isolated", "sticky"],
                    "default": "isolated",
                },
            },
            "required": ["target", "prompt"],
        },
    },
    {
        "name": "list_agents",
        "description": (
            "List the domain specialist agents available in this tenant. "
            "Returns each specialist's name, id, and short description. "
            "Use when unsure which specialist matches the user's request, "
            "or to refresh your view of available agents (the catalog you "
            "received at spawn may be stale if specialists changed since)."
        ),
        "parameters": {"type": "object", "properties": {}},
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
    # Frontdesk v1.2: forbid send_message from inside a delegated call.
    # A delegate runs in a child conversation on an ``internal`` channel
    # binding; routing a fresh outbound message to that chat would either
    # be silently dropped (WebUI gateway doesn't subscribe to internal
    # bindings) or pollute parent UX. The delegate's reply path is the RPC
    # response, not this tool.
    if ctx.role_config.get("is_delegated_call"):
        return _text_result(
            "send_message is not allowed inside a delegated call. "
            "Your reply travels back to the frontdesk as the tool result.",
            is_error=True,
        )
    data: dict[str, Any] = {
        "type": "message",
        "chatJid": ctx.chat_jid,
        "text": args["text"],
        "groupFolder": ctx.group_folder,
        "tenantId": ctx.tenant_id,
        "coworkerId": ctx.coworker_id,
        # Scheduled-task fires lose the natural-output dedup path the
        # orchestrator relies on (see ``_handle_agent_message_ipc``);
        # stamp the flag so the orchestrator forwards instead of drops.
        # Default-false flag — interactive turns get the legacy drop
        # behaviour, scheduled tasks get a forward branch.
        "isScheduledTask": ctx.is_scheduled_task,
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
            # v6.1 §P1.7: forward the RoleMesh user behind this turn.
            # The orchestrator's task handler writes it onto
            # ``scheduled_tasks.created_by_user_id`` so the run-time
            # ``AgentInput.user_id`` can be reconstructed at fire time.
            "userId": ctx.user_id,
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
            if ctx.can_manage_others
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
    except Exception as exc:  # noqa: BLE001
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


MAX_DELEGATE_PROMPT_CHARS = 16_000


async def delegate_to_agent(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Synchronously hand the user's request to a domain specialist.

    Performs the agent-side validation that doesn't need orchestrator
    state (permission gate, prompt length, context_mode shape) before
    making the core NATS RPC. The 320s timeout matches the business
    deadline (300s) + a small buffer; tests that exercise the slow-LLM
    path mock the orchestrator's side rather than waiting here.
    """
    if not ctx.permissions.get("agent_delegate"):
        return _text_result(
            "Permission denied: agent_delegate is not enabled.",
            is_error=True,
        )
    target = str(args.get("target") or "").strip()
    prompt = str(args.get("prompt") or "")
    context_mode = args.get("context_mode") or "isolated"
    if not target or not prompt:
        return _text_result(
            "target and prompt are required.", is_error=True
        )
    if len(prompt) > MAX_DELEGATE_PROMPT_CHARS:
        return _text_result(
            f"prompt exceeds {MAX_DELEGATE_PROMPT_CHARS} chars "
            f"({len(prompt)} given). Self-contained prompts must fit in "
            "one tool call — split the task into smaller delegations "
            "(e.g. one call per question or per document section), or "
            "ask the user to upload long content via file tools that "
            "the specialist can read directly. Do NOT retry with the "
            "same oversized prompt.",
            is_error=True,
        )
    if context_mode not in ("isolated", "sticky"):
        return _text_result(
            "context_mode must be 'isolated' or 'sticky'.",
            is_error=True,
        )

    # Server enforces MAX_DELEGATION_DEPTH (handbook §6 Step 5.3); this
    # value is just what the caller knows about itself. role_config is
    # typed ``dict[str, object]`` so we narrow defensively — a malformed
    # value falls back to 0 rather than crashing the tool call.
    raw_depth = ctx.role_config.get("delegation_depth")
    depth = raw_depth if isinstance(raw_depth, int) else 0

    payload = {
        "type": "delegate_to_agent",
        "tenantId": ctx.tenant_id,
        "fromCoworkerId": ctx.coworker_id,
        "fromConversationId": ctx.conversation_id,
        "userId": ctx.user_id or None,
        "target": target,
        "prompt": prompt,
        "contextMode": context_mode,
        "depth": depth,
    }
    try:
        resp = await ctx.request(
            f"agent.{ctx.job_id}.delegate.request",
            payload,
            timeout=320.0,
        )
    except TimeoutError:
        return _text_result(
            f"Delegation to {target!r} timed out at the RPC layer.",
            is_error=True,
        )
    return _text_result(
        str(resp.get("text", "")),
        is_error=bool(resp.get("isError", False)),
    )


async def list_agents(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Refresh the domain-specialist roster mid-turn (Frontdesk v1.2).

    The spawn-time catalog injected into the system prompt may be stale
    when specialists were added or removed since the frontdesk
    container started. This tool hits the orchestrator's core NATS RPC
    responder for a fresh roster. Always returns either the catalog
    text or an explicit error string — never raises out of the tool.
    """
    payload = {
        "tenantId": ctx.tenant_id,
        "fromCoworkerId": ctx.coworker_id,
    }
    try:
        resp = await ctx.request(
            f"agent.{ctx.job_id}.list_agents.request",
            payload,
            timeout=10.0,
        )
    except TimeoutError:
        return _text_result("list_agents timed out.", is_error=True)
    text = str(resp.get("text", ""))
    if resp.get("error"):
        return _text_result(
            f"list_agents failed: {resp['error']}", is_error=True
        )
    return _text_result(text)


# Map tool names to their implementation functions.
TOOL_FUNCTIONS: dict[str, Any] = {
    "send_message": send_message,
    "schedule_task": schedule_task,
    "list_tasks": list_tasks,
    "pause_task": pause_task,
    "resume_task": resume_task,
    "cancel_task": cancel_task,
    "update_task": update_task,
    "list_agents": list_agents,
    "delegate_to_agent": delegate_to_agent,
}
