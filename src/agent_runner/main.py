"""
RoleMesh Agent Runner (Python)

Runs inside a Docker container with NATS-based IPC.

Input protocol:
  NATS KV: Reads initial config from KV bucket "agent-init" key JOB_ID
  NATS JetStream: Follow-up messages via agent.{JOB_ID}.input
  NATS request-reply: Close signal via agent.{JOB_ID}.close

Output protocol:
  NATS JetStream: Results published to agent.{JOB_ID}.results
  NATS JetStream: Messages and tasks via agent.{JOB_ID}.messages / .tasks
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nats
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
from rolemesh_ipc_protocol import AgentInitData, McpServerSpec

from .ipc_mcp import create_rolemesh_mcp_server

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nats.aio.client import Client
    from nats.js.client import JetStreamContext

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class ContainerInput:
    prompt: str
    group_folder: str
    chat_jid: str
    permissions: dict[str, object]
    user_id: str = ""
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None


@dataclass
class ContainerOutput:
    status: str  # "success" | "error"
    result: str | None
    new_session_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "result": self.result}
        if self.new_session_id is not None:
            d["newSessionId"] = self.new_session_id
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass
class SessionEntry:
    session_id: str
    full_path: str
    summary: str
    first_prompt: str


@dataclass
class ParsedMessage:
    role: str  # "user" | "assistant"
    content: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
JOB_ID = os.environ.get("JOB_ID", "")


# ---------------------------------------------------------------------------
# MessageStream -- push-based async iterable for SDK user messages
# ---------------------------------------------------------------------------


class MessageStream:
    """
    Push-based async iterable for streaming user messages to the SDK.
    Keeps the iterable alive until end() is called, preventing isSingleUserTurn.
    """

    def __init__(self) -> None:
        self._queue: list[dict[str, Any]] = []
        self._event: asyncio.Event = asyncio.Event()
        self._done: bool = False

    def push(self, text: str) -> None:
        self._queue.append(
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": "",
            }
        )
        self._event.set()

    def end(self) -> None:
        self._done = True
        self._event.set()

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            while self._queue:
                yield self._queue.pop(0)
            if self._done:
                return
            self._event.clear()
            await self._event.wait()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


def get_session_summary(session_id: str, transcript_path: str) -> str | None:
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"

    if not index_path.exists():
        log(f"Sessions index not found at {index_path}")
        return None

    try:
        index_data = json.loads(index_path.read_text())
        for entry in index_data.get("entries", []):
            if entry.get("sessionId") == session_id:
                summary = entry.get("summary")
                if summary:
                    return summary
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        log(f"Failed to read sessions index: {exc}")

    return None


# ---------------------------------------------------------------------------
# Transcript archiving (PreCompact hook)
# ---------------------------------------------------------------------------


def _sanitize_filename(summary: str) -> str:
    import re

    name = summary.lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    return name[:50]


def _generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def parse_transcript(content: str) -> list[ParsedMessage]:
    messages: list[ParsedMessage] = []

    for line in content.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                msg_content = entry["message"]["content"]
                text = msg_content if isinstance(msg_content, str) else "".join(c.get("text", "") for c in msg_content)
                if text:
                    messages.append(ParsedMessage(role="user", content=text))
            elif entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                text_parts = [c.get("text", "") for c in entry["message"]["content"] if c.get("type") == "text"]
                text = "".join(text_parts)
                if text:
                    messages.append(ParsedMessage(role="assistant", content=text))
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    return messages


def format_transcript_markdown(
    messages: list[ParsedMessage],
    title: str | None = None,
    assistant_name: str | None = None,
) -> str:
    now = datetime.now()
    date_str = now.strftime("%b %-d, %-I:%M %p")

    lines: list[str] = [
        f"# {title or 'Conversation'}",
        "",
        f"Archived: {date_str}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        sender = "User" if msg.role == "user" else (assistant_name or "Assistant")
        content = msg.content[:2000] + "..." if len(msg.content) > 2000 else msg.content
        lines.append(f"**{sender}**: {content}")
        lines.append("")

    return "\n".join(lines)


def create_pre_compact_hook(
    assistant_name: str | None = None,
) -> Any:
    """Return a PreCompact hook callback that archives transcripts."""

    async def hook(input_data: Any, _tool_use_id: Any, _context: Any) -> dict[str, Any]:
        transcript_path: str | None = getattr(input_data, "transcript_path", None)
        session_id: str | None = getattr(input_data, "session_id", None)

        if not transcript_path or not Path(transcript_path).exists():
            log("No transcript found for archiving")
            return {}

        try:
            content = Path(transcript_path).read_text()
            messages = parse_transcript(content)

            if not messages:
                log("No messages to archive")
                return {}

            summary = get_session_summary(session_id, transcript_path) if session_id else None
            name = _sanitize_filename(summary) if summary else _generate_fallback_name()

            conversations_dir = Path("/workspace/group/conversations")
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = datetime.now().strftime("%Y-%m-%d")
            filename = f"{date}-{name}.md"
            filepath = conversations_dir / filename

            markdown = format_transcript_markdown(messages, summary, assistant_name)
            filepath.write_text(markdown)

            log(f"Archived conversation to {filepath}")
        except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
            log(f"Failed to archive transcript: {exc}")

        return {}

    return hook


# ---------------------------------------------------------------------------
# NATS helpers for IPC
# ---------------------------------------------------------------------------


async def publish_output(js: JetStreamContext, job_id: str, output: ContainerOutput) -> None:
    """Publish a result to JetStream (Channel 2)."""
    await js.publish(
        f"agent.{job_id}.results",
        json.dumps(output.to_dict()).encode(),
    )


async def wait_for_nats_message(
    nc: Client,
    js: JetStreamContext,
    job_id: str,
) -> str | None:
    """Wait for a new NATS input message or close signal.

    Returns the message text, or None if close signal received.
    """
    result_text: str | None = None
    close_received = asyncio.Event()
    message_received = asyncio.Event()

    # Subscribe to follow-up messages (Channel 3)
    input_sub = await js.subscribe(f"agent.{job_id}.input")

    # Subscribe to close signal (Channel 3 - request-reply)
    async def handle_close(msg: Any) -> None:
        await msg.respond(b"ack")
        close_received.set()

    close_sub = await nc.subscribe(f"agent.{job_id}.close", cb=handle_close)

    try:
        while True:
            # Check for input messages
            try:
                msg = await asyncio.wait_for(input_sub.next_msg(timeout=0.5), timeout=0.5)
                data = json.loads(msg.data)
                await msg.ack()
                if data.get("type") == "message" and data.get("text"):
                    result_text = data["text"]
                    message_received.set()
            except TimeoutError:
                pass

            if close_received.is_set():
                return None
            if message_received.is_set():
                return result_text
    finally:
        await input_sub.unsubscribe()
        await close_sub.unsubscribe()


async def drain_nats_input(js: JetStreamContext, job_id: str) -> list[str]:
    """Drain any pending input messages from NATS."""
    messages: list[str] = []
    sub = await js.subscribe(f"agent.{job_id}.input")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(sub.next_msg(timeout=0.1), timeout=0.1)
                data = json.loads(msg.data)
                await msg.ack()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except TimeoutError:
                break
    finally:
        await sub.unsubscribe()
    return messages


# ---------------------------------------------------------------------------
# Core query runner
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    new_session_id: str | None = None
    last_assistant_uuid: str | None = None
    closed_during_query: bool = False


async def run_query(
    prompt: str,
    session_id: str | None,
    mcp_server: Any,
    container_input: ContainerInput,
    sdk_env: dict[str, str | None],
    nc: Client,
    js: JetStreamContext,
    job_id: str,
    resume_at: str | None = None,
    coworker_system_prompt: str | None = None,
    mcp_servers: list[McpServerSpec] | None = None,
) -> QueryResult:
    stream = MessageStream()
    stream.push(prompt)

    result = QueryResult()
    ipc_polling = True

    # Subscribe to close signal
    close_received = asyncio.Event()

    async def handle_close(msg: Any) -> None:
        await msg.respond(b"ack")
        close_received.set()

    close_sub = await nc.subscribe(f"agent.{job_id}.close", cb=handle_close)

    # Subscribe to follow-up input messages
    input_sub = await js.subscribe(f"agent.{job_id}.input")

    async def poll_nats_during_query() -> None:
        nonlocal ipc_polling
        while ipc_polling:
            if close_received.is_set():
                log("Close signal detected during query, ending stream")
                result.closed_during_query = True
                stream.end()
                ipc_polling = False
                return
            try:
                msg = await asyncio.wait_for(input_sub.next_msg(timeout=0.5), timeout=0.5)
                data = json.loads(msg.data)
                await msg.ack()
                if data.get("type") == "message" and data.get("text"):
                    text = data["text"]
                    log(f"Piping NATS message into active query ({len(text)} chars)")
                    stream.push(text)
            except TimeoutError:
                pass

    # Load global CLAUDE.md as additional system context (shared across all groups)
    global_claude_md_path = Path("/workspace/global/CLAUDE.md")
    global_claude_md: str | None = None
    if container_input.permissions.get("data_scope") != "tenant" and global_claude_md_path.exists():
        global_claude_md = global_claude_md_path.read_text()

    # Discover additional directories mounted at /workspace/extra/*
    extra_dirs: list[str] = []
    extra_base = Path("/workspace/extra")
    if extra_base.exists():
        for entry in extra_base.iterdir():
            if entry.is_dir():
                extra_dirs.append(str(entry))
    if extra_dirs:
        log(f"Additional directories: {', '.join(extra_dirs)}")

    # Build system prompt from coworker config + global CLAUDE.md
    system_prompt: dict[str, Any] | None = None
    append_parts: list[str] = []
    if coworker_system_prompt:
        append_parts.append(coworker_system_prompt)
    if global_claude_md:
        append_parts.append(global_claude_md)
    if append_parts:
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(append_parts),
        }

    # Build extra_args for resume-session-at
    extra_args: dict[str, str] | None = None
    if resume_at:
        extra_args = {"resume-session-at": resume_at}

    # Build MCP servers dict — start with the built-in rolemesh server
    mcp_servers_dict: dict[str, Any] = {"rolemesh": mcp_server}

    # Build allowed tools list
    allowed_tools = [
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "Task",
        "TaskOutput",
        "TaskStop",
        "TeamCreate",
        "TeamDelete",
        "SendMessage",
        "TodoWrite",
        "ToolSearch",
        "Skill",
        "NotebookEdit",
        "mcp__rolemesh__*",
    ]

    # Register external MCP servers from init data
    if mcp_servers:
        for spec in mcp_servers:
            mcp_servers_dict[spec.name] = {
                "type": spec.type,
                "url": spec.url,
            }
            allowed_tools.append(f"mcp__{spec.name}__*")
            log(f"External MCP server registered: {spec.name} ({spec.type}) → {spec.url}")

    options = ClaudeAgentOptions(
        cwd="/workspace/group",
        add_dirs=extra_dirs if extra_dirs else None,
        resume=session_id,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        env=sdk_env,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_servers_dict,
        hooks={
            "PreCompact": [HookMatcher(hooks=[create_pre_compact_hook(container_input.assistant_name)])],
        },
        setting_sources=["project", "user"],
    )

    if extra_args:
        options.extra_args = extra_args

    message_count = 0
    result_count = 0

    # Start NATS polling as a background task
    poll_task = asyncio.ensure_future(poll_nats_during_query())

    try:
        async for message in query(prompt=stream, options=options):
            message_count += 1

            cls_name = type(message).__name__
            log_type = cls_name

            if cls_name == "SystemMessage":
                subtype = getattr(message, "subtype", "")
                log_type = f"system/{subtype}"
                data = getattr(message, "data", {})

                if subtype == "init":
                    result.new_session_id = data.get("session_id") if isinstance(data, dict) else None
                    log(f"Session initialized: {result.new_session_id}")

                elif subtype == "task_notification":
                    log(
                        f"Task notification: task={data.get('task_id')} "
                        f"status={data.get('status')} summary={data.get('summary')}"
                        if isinstance(data, dict)
                        else f"Task notification: {data}"
                    )

            elif cls_name == "AssistantMessage":
                uuid = getattr(message, "uuid", None)
                if uuid:
                    result.last_assistant_uuid = uuid

            elif cls_name == "ResultMessage":
                result_count += 1
                text_result = getattr(message, "result", None)
                subtype = getattr(message, "subtype", "")
                session_id_from_result = getattr(message, "session_id", None)
                if session_id_from_result:
                    result.new_session_id = session_id_from_result
                preview = text_result[:200] if text_result else ""
                log(f"Result #{result_count}: subtype={subtype}{f' text={preview}' if text_result else ''}")
                await publish_output(
                    js,
                    job_id,
                    ContainerOutput(
                        status="success",
                        result=text_result or None,
                        new_session_id=result.new_session_id,
                    ),
                )

            log(f"[msg #{message_count}] type={log_type}")
    finally:
        # Stop NATS polling once the query iterator ends
        ipc_polling = False
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        await input_sub.unsubscribe()
        await close_sub.unsubscribe()

    log(
        f"Query done. Messages: {message_count}, results: {result_count}, "
        f"lastAssistantUuid: {result.last_assistant_uuid or 'none'}, "
        f"closedDuringQuery: {result.closed_during_query}"
    )
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    if not JOB_ID:
        log("JOB_ID environment variable not set")
        sys.exit(1)

    # Connect to NATS
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    log(f"Connected to NATS at {NATS_URL}")

    # Channel 1: Read initial input from KV
    try:
        kv = await js.key_value("agent-init")
        entry = await kv.get(JOB_ID)
        init = AgentInitData.deserialize(entry.value)
        container_input = ContainerInput(
            prompt=init.prompt,
            group_folder=init.group_folder,
            chat_jid=init.chat_jid,
            permissions=init.permissions,
            user_id=init.user_id,
            session_id=init.session_id,
            is_scheduled_task=init.is_scheduled_task,
            assistant_name=init.assistant_name,
        )
        log(f"Received input for group: {container_input.group_folder}")
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        log(f"Failed to read initial input from NATS KV: {exc}")
        await publish_output(
            js,
            JOB_ID,
            ContainerOutput(
                status="error",
                result=None,
                error=f"Failed to read input from NATS KV: {exc}",
            ),
        )
        await nc.close()
        sys.exit(1)

    # Credentials are injected by the host's credential proxy via ANTHROPIC_BASE_URL.
    sdk_env: dict[str, str | None] = dict(os.environ)

    # Create in-process MCP server
    mcp_server = create_rolemesh_mcp_server(
        chat_jid=container_input.chat_jid,
        group_folder=container_input.group_folder,
        permissions=container_input.permissions,
        js=js,
        job_id=JOB_ID,
        tenant_id=init.tenant_id,
        coworker_id=init.coworker_id,
        conversation_id=init.conversation_id,
    )

    session_id = container_input.session_id

    # Build initial prompt (drain any pending NATS messages too)
    prompt = container_input.prompt
    if container_input.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    pending = await drain_nats_input(js, JOB_ID)
    if pending:
        log(f"Draining {len(pending)} pending NATS messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Query loop: run query -> wait for NATS message -> run new query -> repeat
    resume_at: str | None = None
    try:
        while True:
            log(f"Starting query (session: {session_id or 'new'}, resumeAt: {resume_at or 'latest'})...")

            query_result = await run_query(
                prompt,
                session_id,
                mcp_server,
                container_input,
                sdk_env,
                nc,
                js,
                JOB_ID,
                resume_at,
                coworker_system_prompt=init.system_prompt,
                mcp_servers=init.mcp_servers,
            )
            if query_result.new_session_id:
                session_id = query_result.new_session_id
            if query_result.last_assistant_uuid:
                resume_at = query_result.last_assistant_uuid

            # If close was consumed during the query, exit immediately.
            if query_result.closed_during_query:
                log("Close signal consumed during query, exiting")
                break

            # Emit session update so host can track it
            await publish_output(
                js,
                JOB_ID,
                ContainerOutput(
                    status="success",
                    result=None,
                    new_session_id=session_id,
                ),
            )

            log("Query ended, waiting for next NATS message...")

            # Wait for the next message or close signal
            next_message = await wait_for_nats_message(nc, js, JOB_ID)
            if next_message is None:
                log("Close signal received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message
    except (OSError, json.JSONDecodeError, KeyError, ValueError, RuntimeError) as exc:
        error_message = str(exc)
        log(f"Agent error: {error_message}")
        await publish_output(
            js,
            JOB_ID,
            ContainerOutput(
                status="error",
                result=None,
                new_session_id=session_id,
                error=error_message,
            ),
        )
        await nc.close()
        sys.exit(1)

    await nc.close()
