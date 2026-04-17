"""
AgentBackend protocol — the contract between the NATS bridge and backend implementations.

Each backend (Claude SDK, Pi) implements this protocol. The NATS bridge in
main.py translates BackendEvents into NATS publishes without knowing which
backend produced them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

    from .tools.context import ToolContext


# ---------------------------------------------------------------------------
# Events emitted by backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultEvent:
    """A (possibly intermediate) result from the agent."""

    text: str | None
    new_session_id: str | None = None


@dataclass(frozen=True)
class SessionInitEvent:
    """Emitted once when the backend has established a session."""

    session_id: str


@dataclass(frozen=True)
class CompactionEvent:
    """Emitted when the backend is about to compact / archive transcripts."""

    transcript_path: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class ErrorEvent:
    """Emitted on unrecoverable backend errors."""

    error: str


@dataclass(frozen=True)
class RunningEvent:
    """Emitted once when the backend session is ready to process prompts."""


@dataclass(frozen=True)
class ToolUseEvent:
    """Emitted when the agent starts invoking a tool."""

    tool: str
    input_preview: str = ""


@dataclass(frozen=True)
class StoppedEvent:
    """Emitted after abort() has halted the current turn.

    Sent as confirmation to the UI so the Stop button can exit the
    'stopping' transitional state. The container remains alive and
    ready to receive the next prompt.
    """


BackendEvent = (
    ResultEvent
    | SessionInitEvent
    | CompactionEvent
    | ErrorEvent
    | RunningEvent
    | ToolUseEvent
    | StoppedEvent
)


def tool_input_preview(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Extract a short user-facing preview from a tool call's input dict.

    Handles both Claude Code SDK (PascalCase: "Bash", "Read") and Pi
    (lowercase: "bash", "read") tool naming conventions. MCP tools arrive
    namespaced as "mcp__<server>__<tool>" — strip the prefix so the base
    name can hit the match table below.
    """
    # Namespaced MCP tools: mcp__<server>__<tool> → <tool>
    base = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
    tn = base.lower()
    if tn in ("read", "write", "edit", "glob", "grep", "notebookedit"):
        val = (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("pattern")
            or ""
        )
        return str(val)[:80]
    if tn == "bash":
        return str(tool_input.get("command", ""))[:80]
    if tn in ("websearch", "webfetch"):
        val = tool_input.get("query") or tool_input.get("url") or ""
        return str(val)[:80]
    if tn in ("task", "taskoutput", "taskstop"):
        val = tool_input.get("description") or tool_input.get("taskId") or ""
        return str(val)[:80]
    return ""


# ---------------------------------------------------------------------------
# AgentBackend protocol
# ---------------------------------------------------------------------------


class AgentBackend(Protocol):
    """Thin interface that each backend must implement."""

    async def start(
        self,
        init: AgentInitData,
        tool_ctx: ToolContext,
        mcp_servers: list[McpServerSpec] | None = None,
    ) -> None:
        """Initialize the backend (create sessions, load tools, etc.)."""
        ...

    async def run_prompt(self, text: str) -> None:
        """Run a prompt through the agent. Results arrive via events."""
        ...

    async def handle_follow_up(self, text: str) -> None:
        """Handle a follow-up message (may arrive while agent is running)."""
        ...

    def subscribe(self, listener: Any) -> None:
        """Register an async callback for BackendEvents.

        The callback signature is: async def on_event(event: BackendEvent) -> None
        """
        ...

    async def abort(self) -> None:
        """Abort current execution."""
        ...

    async def shutdown(self) -> None:
        """Clean up resources."""
        ...

    @property
    def session_id(self) -> str | None:
        """Current session ID (backend-specific format)."""
        ...
