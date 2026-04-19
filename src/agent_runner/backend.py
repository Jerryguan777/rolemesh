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

    from .hooks import HookRegistry
    from .tools.context import ToolContext


# ---------------------------------------------------------------------------
# Events emitted by backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultEvent:
    """A reply the agent produced for one user prompt.

    When a prompt batch contains multiple user messages (e.g. a follow-up was
    queued during an active turn), the backend emits one ResultEvent per
    answered user message. `is_final=False` means "more user messages in this
    batch may still be answered"; `is_final=True` marks the end of the batch.
    The NATS bridge uses `is_final` to gate host-side scheduling side effects
    (notify_idle) so they only fire once per run_prompt call, not per reply.
    """

    text: str | None
    new_session_id: str | None = None
    is_final: bool = True


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
    """Thin interface that each backend must implement.

    New backend authors: BEFORE wiring up abort(), read
    `docs/backend-stop-contract.md` (in the repo root) — it lists the
    behaviors Stop must produce end-to-end. Getting stop wrong is easy
    and the failure modes are silent (late replies, leaked context into
    the next turn, ghost follow-ups resurfacing turns later).
    """

    async def start(
        self,
        init: AgentInitData,
        tool_ctx: ToolContext,
        mcp_servers: list[McpServerSpec] | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        """Initialize the backend (create sessions, load tools, etc.).

        `hooks` is the unified HookRegistry. Implementations MUST treat
        hooks=None as an empty registry (no handlers), NOT as a silent
        disable of the hook system — callers like main.py always pass a
        real registry, so a None here is typically a misuse that would
        otherwise lose the transcript archive on compaction. The default
        of None exists only for legacy tests that construct a backend
        directly without building a registry.
        """
        ...

    async def run_prompt(self, text: str) -> None:
        """Run a prompt through the agent. Results arrive via events."""
        ...

    async def handle_follow_up(self, text: str) -> None:
        """Handle a follow-up message (may arrive while agent is running).

        Implementations MUST reject follow-ups that arrive after abort()
        has started and before run_prompt has returned — otherwise the
        in-flight message can race onto whatever queue/stream the backend
        uses to feed the provider, and the cancelled turn's context ends
        up concatenated with the new user message.
        """
        ...

    def subscribe(self, listener: Any) -> None:
        """Register an async callback for BackendEvents.

        The callback signature is: async def on_event(event: BackendEvent) -> None
        """
        ...

    async def abort(self) -> None:
        """Abort current execution. See docs/backend-stop-contract.md.

        Summary of the contract a backend's abort() must deliver:

          1. Stop the underlying provider call. stream.end() / setting a
             cooperative signal is not enough — you must ensure no further
             BackendEvent is emitted for the aborted turn (no late
             ResultEvent, no late ToolUseEvent).
          2. Rewind the "resume anchor" the backend uses to chain turns
             (session tree leaf, conversation uuid, thread id, whatever)
             back to the pre-prompt value. Otherwise the NEXT turn's
             context chains through the aborted message and the provider
             conflates Q1's cancelled question with Q2's fresh one.
          3. Clear any internal queues that buffered follow-ups / steering
             / pending-next-turn messages for the aborted turn. Queue
             residue resurfaces as phantom replies on subsequent turns.
          4. Emit StoppedEvent so the UI can exit the 'stopping' state.
          5. Leave the backend usable for the next turn (container stays
             alive, no latent flags gagging future follow-ups).

        Pi backend uses cooperative cancellation (signal event) + session
        tree leaf rewind. Claude backend uses preemptive cancellation
        (task.cancel) + resume-session-at uuid rewind. The mechanics
        differ; the OBSERVABLE contract above is the same.
        """
        ...

    async def shutdown(self) -> None:
        """Clean up resources."""
        ...

    @property
    def session_id(self) -> str | None:
        """Current session ID (backend-specific format)."""
        ...
