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


BackendEvent = ResultEvent | SessionInitEvent | CompactionEvent | ErrorEvent


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
