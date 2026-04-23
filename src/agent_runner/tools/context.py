"""Shared context for RoleMesh IPC tools."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
    job_id: str
    chat_jid: str
    group_folder: str
    permissions: dict[str, object]
    tenant_id: str
    coworker_id: str
    conversation_id: str
    # Identity of the user whose turn the agent is executing. Passed through
    # to MCP calls via X-RoleMesh-User-Id and recorded on approval requests
    # so the approval path can attribute the proposal to the originating user.
    user_id: str = ""

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

    @property
    def has_tenant_scope(self) -> bool:
        return self.permissions.get("data_scope") == "tenant"

    @property
    def can_schedule(self) -> bool:
        return bool(self.permissions.get("task_schedule"))

    def get_tool_reversibility(self, tool_name: str) -> bool:
        """Return True iff the tool is known to be reversible.

        P0.1 fail-safe stub: always returns False so the safety
        pipeline treats every tool as irreversible. P0.4 replaces
        this with a real lookup against the builtin reversibility
        table and per-MCP-server overrides. Keeping the method here
        now lets the hook handler call it unconditionally without
        a hasattr dance later.
        """
        del tool_name
        return False
