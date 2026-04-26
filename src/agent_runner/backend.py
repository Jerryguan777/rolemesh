"""
AgentBackend protocol — the contract between the NATS bridge and backend implementations.

Each backend (Claude SDK, Pi) implements this protocol. The NATS bridge in
main.py translates BackendEvents into NATS publishes without knowing which
backend produced them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from rolemesh.ipc.protocol import AgentInitData, McpServerSpec

    from .hooks import HookRegistry
    from .tools.context import ToolContext


# ---------------------------------------------------------------------------
# Events emitted by backends
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageSnapshot:
    """Per-turn LLM usage attached to terminal backend events.

    Carried on ResultEvent / ErrorEvent / StoppedEvent / SafetyBlockEvent so
    downstream consumers can persist what a turn cost in tokens and USD.
    The *_tokens suffix is intentional — it keeps the field names from
    being confused with a USD amount.

    cost_usd is None whenever the backend can't attribute cost for this
    turn (model not in the price registry, custom proxy that doesn't
    compute pricing, exception path that lost the snapshot). When it IS
    populated, ``cost_source`` distinguishes the provenance.

    cost_source documents where the cost number originated:
      * "sdk"      — Claude Agent SDK's total_cost_usd (authoritative for
                     subscription billing; computed inside the SDK).
      * "provider" — Pi backend's per-provider price-table calculation
                     (``pi.ai.models.calculate_cost``). Subject to the
                     known limitations of the upstream price table:
                     no long-context tier pricing, single ``cache_write``
                     rate that under-bills 1-hour TTL caches, and
                     un-registered custom models that produce cost=None.
      * None       — cost_usd is None.

    model_id identifies which model produced this turn (informative when a
    Coworker fans out across providers; for Pi it's the dominant model in
    the prompt by output_tokens — see _PromptUsageAccumulator).

    Failed and aborted turns still populate this — LLM tokens spent before
    a turn was cancelled or rejected are not refunded by the provider, so
    only attaching usage to ResultEvent would systematically under-count.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    model_id: str | None = None
    cost_source: Literal["sdk", "provider"] | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Serialize to the wire dict carried inside ContainerOutput.metadata.

        Kept symmetric with from_metadata. Keys mirror the field names so the
        DB-side decoding stays a one-line dict lookup; cost_usd / model_id /
        cost_source serialize as None when unset (rather than being omitted)
        so the receiver can tell "backend reported zero" apart from "backend
        did not report".
        """
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
            "model_id": self.model_id,
            "cost_source": self.cost_source,
        }

    @classmethod
    def from_metadata(cls, data: dict[str, Any]) -> UsageSnapshot:
        cost_source_raw = data.get("cost_source")
        cost_source: Literal["sdk", "provider"] | None
        cost_source = (
            cost_source_raw if cost_source_raw in ("sdk", "provider") else None
        )
        cost_usd_raw = data.get("cost_usd")
        cost_usd = float(cost_usd_raw) if isinstance(cost_usd_raw, (int, float)) else None
        model_id_raw = data.get("model_id")
        model_id = model_id_raw if isinstance(model_id_raw, str) else None
        return cls(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_read_tokens=int(data.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(data.get("cache_write_tokens", 0) or 0),
            cost_usd=cost_usd,
            model_id=model_id,
            cost_source=cost_source,
        )


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
    usage: UsageSnapshot | None = None


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
    """Emitted on unrecoverable backend errors.

    ``usage`` carries the tokens already burned on the failed turn — most
    LLM providers bill prompt tokens even on partial completions, so
    leaving it blank would systematically under-report cost. Backends
    that can't recover usage on the failure path (e.g. the Claude SDK
    has no per-turn usage on its exception path) leave it None.
    """

    error: str
    usage: UsageSnapshot | None = None


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

    ``usage`` carries the tokens spent on the aborted turn so cost
    accounting doesn't lose them. None when the backend cannot recover
    a usage snapshot at abort time.
    """

    usage: UsageSnapshot | None = None


@dataclass(frozen=True)
class SafetyBlockEvent:
    """Emitted when the safety framework blocks a turn or tool call.

    Kept distinct from ResultEvent so the full pipeline — orchestrator
    _on_output, DB message persistence, metrics, WebSocket frames, and
    UI rendering — can tell "safety intercepted this" apart from "the
    LLM produced this text". Pre-this-event, both backends forged a
    ResultEvent with the block reason as its text, which polluted the
    messages table with fake assistant replies and made metrics lie.

    ``stage`` is the Stage enum value from rolemesh.safety.types
    (``input_prompt`` / ``pre_tool_call`` / ``model_output`` / ...).
    ``rule_id`` is the UUID of the rule that fired, when one is
    available (a hook-system-error fallback leaves it None).
    ``reason`` is the human-readable message shown in the UI and
    recorded in safety_decisions.
    """

    stage: str
    reason: str
    rule_id: str | None = None
    # Pre-LLM blocks (stage="input_prompt") leave usage None — no model
    # call happened. Output-stage blocks ("model_output") emit usage with
    # the tokens the LLM call consumed before its output was rejected, so
    # billing telemetry doesn't lose those tokens to the safety pipeline.
    usage: UsageSnapshot | None = None


BackendEvent = (
    ResultEvent
    | SessionInitEvent
    | CompactionEvent
    | ErrorEvent
    | RunningEvent
    | ToolUseEvent
    | StoppedEvent
    | SafetyBlockEvent
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
