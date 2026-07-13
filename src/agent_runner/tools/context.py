"""Shared context for RoleMesh IPC tools."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nats.aio.client import Client as NATSClient
    from nats.js.client import JetStreamContext


def _publish_done(task: asyncio.Task[None]) -> None:
    """Log unhandled exceptions from fire-and-forget publishes."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(f"[tools] NATS publish error: {exc}", file=sys.stderr, flush=True)


@dataclass
class ToolContext:
    """Shared runtime context for all RoleMesh IPC tools."""

    js: JetStreamContext
    # Frontdesk v1.2: raw NATS client for core request-reply. JetStream
    # (``js`` above) is fire-and-forget; delegate_to_agent / list_agents
    # need a synchronous reply, which only core NATS request-reply
    # provides without a new JetStream consumer. Same connection reused.
    nc: NATSClient
    job_id: str
    chat_jid: str
    group_folder: str
    permissions: dict[str, object]
    tenant_id: str
    coworker_id: str
    conversation_id: str
    # Identity of the user whose turn the agent is executing. Passed through
    # to MCP calls via X-RoleMesh-User-Id so downstream systems can attribute
    # the call to the originating user.
    user_id: str = ""
    # True when this turn is a scheduled-task fire (vs. an interactive
    # turn). The send_message tool stamps this on its IPC payload so the
    # orchestrator's ``_handle_agent_message_ipc`` knows whether to forward
    # the message to the channel gateway. Interactive turns already deliver
    # the agent's reply through the natural-output path (``agent.*.results``
    # → ``_on_output``), so forwarding send_message would double-send;
    # scheduled-task turns have no equivalent natural-output delivery
    # (``_run_task``'s ``_on_output`` only forwards when ``result`` is
    # non-empty, but agents typically only call send_message and produce
    # no final ``result``), so forwarding IS the only delivery path.
    is_scheduled_task: bool = False
    # V2 P0.4: per-MCP-server reversibility maps. Keyed by server
    # registered name → {bare_tool_name: reversible}. Forwarded from
    # ``AgentInitData.mcp_servers[i].tool_reversibility`` so the hook
    # handler can answer ``get_tool_reversibility`` without a DB or
    # RPC round-trip. Builtin Claude tools (Read/Edit/Bash/...) are
    # resolved via the shared ``BUILTIN_REVERSIBILITY`` table so
    # consumers do not need to duplicate it into every coworker's
    # MCP config.
    mcp_tool_reversibility: dict[str, dict[str, bool]] = field(
        default_factory=dict
    )
    # Frontdesk v1.2: per-turn IPC hint mirroring
    # ``AgentInitData.role_config`` but normalised to ``{}`` at the
    # construction site so downstream tool code never None-checks. Read
    # by ``delegate_to_agent`` for ``delegation_depth`` and by
    # ``send_message`` to refuse delegated-call cross-talk.
    role_config: dict[str, object] = field(default_factory=dict)

    # Internal: background tasks for fire-and-forget publishes
    _bg_tasks: set[asyncio.Task[None]] | None = None

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        """Fire-and-forget publish to NATS JetStream."""
        if self._bg_tasks is None:
            self._bg_tasks = set()
        tasks = self._bg_tasks
        task = asyncio.ensure_future(
            self.js.publish(subject, json.dumps(data, indent=2).encode())  # type: ignore[arg-type]
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_publish_done)

    async def request(
        self,
        subject: str,
        data: dict[str, Any],
        timeout: float = 320.0,
    ) -> dict[str, Any]:
        """Core NATS request-reply, returning the JSON-decoded reply.

        ``timeout=320s`` matches the delegation business deadline plus
        a small buffer; callers (``list_agents`` etc.) override with a
        shorter value where appropriate. Raises ``asyncio.TimeoutError``
        on the underlying ``NATSClient.request`` timeout — the caller
        catches and converts to a tool-level error reply.
        """
        msg = await self.nc.request(
            subject,
            json.dumps(data).encode(),
            timeout=timeout,
        )
        decoded: Any = json.loads(msg.data.decode())
        if not isinstance(decoded, dict):
            raise ValueError(
                f"NATS reply on {subject!r} must be a JSON object, "
                f"got {type(decoded).__name__}"
            )
        return decoded

    @property
    def can_manage_others(self) -> bool:
        return bool(self.permissions.get("task_manage_others"))

    @property
    def can_schedule(self) -> bool:
        return bool(self.permissions.get("task_schedule"))

    def get_tool_reversibility(self, tool_name: str) -> bool:
        """Return True iff the tool is known to be reversible.

        Lookup priority (via
        ``rolemesh.safety.tool_reversibility.resolve_from_full_tool_name``):
          1. Builtin table for stock Claude tools (Read, Edit, Bash, …).
          2. Per-MCP-server overrides for ``mcp__{server}__{tool}`` names.
          3. ``False`` fail-safe default.
        """
        # Lazy import avoids pulling the rolemesh.safety package into
        # the tool-context module at import time — callers that don't
        # register any safety hooks (zero-rule agents) keep the old
        # cold-start cost.
        # Reversibility overrides are keyed by ORIGINAL remote tool
        # names; if the tool-name contract aliased the LLM-visible name
        # (pi.mcp_naming), restore it before the lookup — otherwise an
        # aliased tool always falls through to the fail-safe False.
        from agent_runner.hooks.handlers.approval import parse_mcp_tool_name
        from rolemesh.safety.tool_reversibility import (
            resolve_from_full_tool_name,
        )

        parsed = parse_mcp_tool_name(tool_name)
        if parsed is not None:
            server, original_tool = parsed
            tool_name = f"mcp__{server}__{original_tool}"

        return resolve_from_full_tool_name(
            tool_name, self.mcp_tool_reversibility
        )
