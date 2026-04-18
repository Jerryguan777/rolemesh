"""AgentSession — Python port of packages/coding-agent/src/core/agent-session.ts.

Core abstraction for agent lifecycle and session management. Shared between all
run modes (interactive, print, RPC). Encapsulates:
- Agent state access
- Event subscription with automatic session persistence
- Model and thinking level management
- Compaction (manual and auto)
- Bash execution
- Session switching and branching
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pi.agent.types import (
    AgentEvent,
    AgentMessage,
    AgentState,
    ThinkingLevel,
)
from pi.ai.types import (
    AssistantMessage,
    ImageContent,
    Message,
    Model,
    TextContent,
    UserMessage,
)
from pi.coding_agent.core.bash_executor import BashExecutorOptions, BashResult, execute_bash
from pi.coding_agent.core.messages import BashExecutionMessage, CustomMessage
from pi.coding_agent.core.session_manager import (
    NewSessionOptions,
    SessionManager,
)

# ---------------------------------------------------------------------------
# Additional event types specific to AgentSession
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactionStartEvent:
    """Fired when auto-compaction begins."""

    reason: Literal["threshold", "overflow"] = "threshold"
    type: Literal["auto_compaction_start"] = "auto_compaction_start"


@dataclass
class AutoCompactionEndEvent:
    """Fired when auto-compaction completes."""

    result: Any = None  # CompactionResult | None
    aborted: bool = False
    will_retry: bool = False
    error_message: str | None = None
    type: Literal["auto_compaction_end"] = "auto_compaction_end"


@dataclass
class AutoRetryStartEvent:
    """Fired when auto-retry begins."""

    attempt: int = 0
    max_attempts: int = 0
    delay_ms: int = 0
    error_message: str = ""
    type: Literal["auto_retry_start"] = "auto_retry_start"


@dataclass
class AutoRetryEndEvent:
    """Fired when auto-retry ends (success or failure)."""

    success: bool = False
    attempt: int = 0
    final_error: str | None = None
    type: Literal["auto_retry_end"] = "auto_retry_end"


# Union of all agent session event types (extends AgentEvent)
AgentSessionEvent = (
    AgentEvent | AutoCompactionStartEvent | AutoCompactionEndEvent | AutoRetryStartEvent | AutoRetryEndEvent
)

AgentSessionEventListener = Callable[[AgentSessionEvent], None]


# ---------------------------------------------------------------------------
# Skill Block Parsing
# ---------------------------------------------------------------------------


@dataclass
class ParsedSkillBlock:
    """Parsed skill block from a user message."""

    name: str
    location: str
    content: str
    user_message: str | None = None


# ---------------------------------------------------------------------------
# Model Cycle Result
# ---------------------------------------------------------------------------


@dataclass
class ModelCycleResult:
    """Result from cycleModel()."""

    model: Model
    thinking_level: ThinkingLevel
    is_scoped: bool = False


# ---------------------------------------------------------------------------
# Extension Bindings
# ---------------------------------------------------------------------------


@dataclass
class ExtensionBindings:
    """Bindings for connecting extensions to the agent session."""

    ui_context: Any = None  # ExtensionUIContext
    command_context_actions: Any = None  # ExtensionCommandContextActions
    shutdown_handler: Callable[[], None] | None = None
    on_error: Callable[[Any], None] | None = None  # ExtensionErrorListener


# Default thinking level
DEFAULT_THINKING_LEVEL: ThinkingLevel = "off"

# snake_case alias for parity with TS camelCase export
default_thinking_level = DEFAULT_THINKING_LEVEL

# Thinking levels available for all reasoning models
THINKING_LEVELS: list[ThinkingLevel] = ["off", "minimal", "low", "medium", "high"]
# Levels including xhigh for supported models
THINKING_LEVELS_WITH_XHIGH: list[ThinkingLevel] = ["off", "minimal", "low", "medium", "high", "xhigh"]

# Retryable error pattern
_RETRYABLE_ERROR_PATTERN = re.compile(
    r"overloaded|rate.?limit|too many requests|429|500|502|503|504|"
    r"service.?unavailable|server error|internal error|connection.?error|"
    r"connection.?refused|other side closed|fetch failed|upstream.?connect|"
    r"reset before headers|terminated|retry delay",
    re.IGNORECASE,
)


@dataclass
class AgentSessionConfig:
    """Configuration for AgentSession."""

    agent: Any  # Agent
    session_manager: SessionManager
    cwd: str
    settings_manager: Any = None  # SettingsManager
    scoped_models: list[Any] | None = None  # list[ScopedModel]
    resource_loader: Any = None  # ResourceLoader
    custom_tools: list[Any] | None = None  # list[ToolDefinition]
    model_registry: Any = None  # ModelRegistry
    initial_active_tool_names: list[str] | None = None
    extension_runner_ref: dict[str, Any] | None = None


@dataclass
class PromptOptions:
    """Options for AgentSession.prompt()."""

    expand_prompt_templates: bool = True
    images: list[ImageContent] | None = None
    streaming_behavior: Literal["steer", "followUp"] | None = None
    source: str | None = None


@dataclass
class SessionStats:
    """Session statistics for reporting."""

    session_file: str | None = None
    session_id: str = ""
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_results: int = 0
    total_messages: int = 0
    tokens: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0


class AgentSession:
    """Core abstraction for agent lifecycle and session management.

    Modes (interactive, print, RPC) use this class and add their own I/O
    layer on top.
    """

    def __init__(self, config: AgentSessionConfig) -> None:
        self._agent: Any = config.agent  # Agent
        self._session_manager = config.session_manager
        self._cwd = config.cwd
        self._settings_manager: Any = config.settings_manager
        self._scoped_models: list[Any] = config.scoped_models or []
        self._resource_loader: Any = config.resource_loader
        self._custom_tools: list[Any] = config.custom_tools or []
        self._model_registry: Any = config.model_registry
        self._initial_active_tool_names = config.initial_active_tool_names
        self._extension_runner_ref: dict[str, Any] | None = config.extension_runner_ref

        # Extension bindings
        self._extension_ui_context: Any = None
        self._extension_command_context_actions: Any = None
        self._extension_shutdown_handler: Callable[[], None] | None = None
        self._extension_error_listener: Callable[[Any], None] | None = None

        # Event listeners
        self._event_listeners: list[AgentSessionEventListener] = []
        self._unsubscribe_agent: Callable[[], None] | None = None

        # Message queues
        self._steering_messages: list[str] = []
        self._follow_up_messages: list[str] = []
        self._pending_next_turn_messages: list[CustomMessage] = []
        self._pending_bash_messages: list[BashExecutionMessage] = []

        # Abort events
        self._compaction_abort_event: asyncio.Event | None = None
        self._auto_compaction_abort_event: asyncio.Event | None = None
        self._bash_abort_event: asyncio.Event | None = None
        self._retry_abort_event: asyncio.Event | None = None

        # Retry state
        self._retry_attempt = 0
        self._retry_future: asyncio.Future[None] | None = None

        # Last assistant message seen (for auto-compaction checks)
        self._last_assistant_message: AssistantMessage | None = None
        # Background task reference for auto-compaction (kept to prevent GC)
        self._compaction_task: asyncio.Future[None] | None = None
        # Leaf id at the instant prompt() begins a turn. On abort, used to
        # rewind the active branch so aborted entries become orphaned and
        # don't leak into the next turn's LLM context. None means no prompt
        # has been captured (e.g. abort arrived between turns — no-op).
        self._pre_prompt_leaf_id: str | None = None

        # Subscribe to agent events
        self._unsubscribe_agent = self._agent.subscribe(self._handle_agent_event)

    # =========================================================================
    # Properties (Shared Interface Contract)
    # =========================================================================

    @property
    def state(self) -> AgentState:
        """Full agent state."""
        return cast(AgentState, self._agent.state)

    @property
    def model(self) -> Model | None:
        """Current model."""
        return getattr(self._agent.state, "model", None)

    @property
    def thinking_level(self) -> ThinkingLevel:
        """Current thinking level."""
        return getattr(self._agent.state, "thinking_level", DEFAULT_THINKING_LEVEL)

    @property
    def is_streaming(self) -> bool:
        """Whether agent is currently streaming."""
        return getattr(self._agent.state, "is_streaming", False)

    @property
    def messages(self) -> list[AgentMessage]:
        """All messages including custom types."""
        return cast(list[AgentMessage], self._agent.state.messages)

    @property
    def session_id(self) -> str:
        """Current session ID."""
        return self._session_manager.get_session_id()

    @property
    def session_file(self) -> str | None:
        """Current session file path."""
        return self._session_manager.get_session_file()

    @property
    def session_manager(self) -> SessionManager:
        """The underlying session manager."""
        return self._session_manager

    @property
    def is_compacting(self) -> bool:
        """Whether auto-compaction is currently running."""
        return self._auto_compaction_abort_event is not None or self._compaction_abort_event is not None

    @property
    def is_bash_running(self) -> bool:
        """Whether a bash command is currently running."""
        return self._bash_abort_event is not None

    @property
    def is_retrying(self) -> bool:
        """Whether auto-retry is currently in progress."""
        return self._retry_future is not None

    @property
    def retry_attempt(self) -> int:
        """Current retry attempt (0 if not retrying)."""
        return self._retry_attempt

    @property
    def pending_message_count(self) -> int:
        """Total count of pending steering and follow-up messages."""
        return len(self._steering_messages) + len(self._follow_up_messages)

    # =========================================================================
    # Event subscription
    # =========================================================================

    def subscribe(self, listener: AgentSessionEventListener) -> Callable[[], None]:
        """Subscribe to agent session events. Returns unsubscribe function."""
        self._event_listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._event_listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: AgentSessionEvent) -> None:
        """Emit an event to all registered listeners."""
        for listener in list(self._event_listeners):
            with contextlib.suppress(Exception):
                listener(event)

    def _handle_agent_event(self, event: AgentEvent) -> None:
        """Internal handler for agent events — persistence, extensions, auto-compaction."""
        # Track pending bash messages
        if getattr(event, "type", None) == "message_start":
            msg = getattr(event, "message", None)
            if msg is not None:
                text = self._get_user_message_text(msg)
                if text:
                    if text in self._steering_messages:
                        self._steering_messages.remove(text)
                    elif text in self._follow_up_messages:
                        self._follow_up_messages.remove(text)

        # Forward to listeners
        self._emit(event)

        # Handle session persistence
        event_type = getattr(event, "type", None)
        if event_type == "message_end":
            msg = getattr(event, "message", None)
            if msg is not None:
                role = getattr(msg, "role", "")
                if role == "custom":
                    self._session_manager.append_custom_message_entry(
                        getattr(msg, "custom_type", ""),
                        getattr(msg, "content", ""),
                        getattr(msg, "display", True),
                        getattr(msg, "details", None),
                    )
                elif role in ("user", "assistant", "toolResult"):
                    self._session_manager.append_message(msg)

                if role == "assistant":
                    self._last_assistant_message = msg

        if event_type == "agent_end" and self._last_assistant_message is not None:
            msg = self._last_assistant_message
            self._last_assistant_message = None
            # Schedule auto-compaction check (don't block event handler)
            self._compaction_task = asyncio.ensure_future(self._check_compaction(msg))

    def _get_user_message_text(self, message: Message) -> str:
        """Extract text content from a user message."""
        if getattr(message, "role", "") != "user":
            return ""
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        return "".join(getattr(b, "text", "") for b in content if isinstance(b, TextContent))

    def _disconnect_from_agent(self) -> None:
        """Temporarily disconnect from agent events."""
        if self._unsubscribe_agent is not None:
            self._unsubscribe_agent()
            self._unsubscribe_agent = None

    def _reconnect_to_agent(self) -> None:
        """Reconnect to agent events."""
        if self._unsubscribe_agent is not None:
            return
        self._unsubscribe_agent = self._agent.subscribe(self._handle_agent_event)

    def dispose(self) -> None:
        """Remove all listeners and disconnect from agent."""
        self._disconnect_from_agent()
        self._event_listeners.clear()

    # =========================================================================
    # Extension bindings
    # =========================================================================

    async def bind_extensions(self, bindings: ExtensionBindings | dict[str, Any]) -> None:
        """Bind extension callbacks to this session.

        Accepts either an ExtensionBindings dataclass or a raw dict
        (for convenience in print/interactive modes).
        """
        if isinstance(bindings, dict):
            bindings = ExtensionBindings(
                ui_context=bindings.get("ui_context"),
                command_context_actions=bindings.get("command_context_actions"),
                shutdown_handler=bindings.get("shutdown_handler"),
                on_error=bindings.get("on_error"),
            )

        if bindings.ui_context is not None:
            self._extension_ui_context = bindings.ui_context
        if bindings.command_context_actions is not None:
            self._extension_command_context_actions = bindings.command_context_actions
        if bindings.shutdown_handler is not None:
            self._extension_shutdown_handler = bindings.shutdown_handler
        if bindings.on_error is not None:
            self._extension_error_listener = bindings.on_error

        runner = self._extension_runner_ref.get("current") if self._extension_runner_ref else None
        if runner is not None:
            await runner.emit({"type": "session_start"})

    # =========================================================================
    # Prompting
    # =========================================================================

    async def prompt(self, text: str, **kwargs: Any) -> None:
        """Send a prompt to the agent.

        Args:
            text: The user message text.
            **kwargs: Optional PromptOptions fields (images, streaming_behavior, etc.)
        """
        images: list[ImageContent] | None = kwargs.get("images")
        streaming_behavior: str | None = kwargs.get("streaming_behavior")

        if self.is_streaming:
            if streaming_behavior is None:
                raise RuntimeError(
                    "Agent is already processing. "
                    "Specify streaming_behavior ('steer' or 'followUp') to queue the message."
                )
            if streaming_behavior == "followUp":
                await self._queue_follow_up(text, images)
            else:
                await self._queue_steer(text, images)
            return

        self._flush_pending_bash_messages()

        if self.model is None:
            raise RuntimeError("No model selected.")

        user_content: list[TextContent | ImageContent] = [TextContent(text=text)]
        if images:
            user_content.extend(images)

        messages: list[Any] = [UserMessage(content=user_content)]

        for msg in self._pending_next_turn_messages:
            messages.append(msg)
        self._pending_next_turn_messages.clear()

        # Snapshot the leaf right before this turn starts appending. If the
        # user aborts, abort() rewinds back to this id so Q1 + any partial
        # aborted assistant become an orphan branch (preserved on disk for
        # debugging, but excluded from the active parent chain).
        self._pre_prompt_leaf_id = self._session_manager.get_leaf_id()

        await self._agent.prompt(messages)

    async def abort(self) -> None:
        """Abort current operation and wait for agent to become idle.

        If the aborted turn produced any entries on disk (user message,
        tool calls, partial aborted assistant), rewind the session's active
        leaf to the pre-prompt snapshot and rebuild the agent's in-memory
        message list from it. Without this the next prompt's LLM context
        would still chain through the aborted user message and Pi would
        visibly conflate the cancelled question with the new one.
        """
        self._abort_retry()
        self._agent.abort()
        await self._agent.wait_for_idle()

        if (
            self._last_assistant_message is not None
            and getattr(self._last_assistant_message, "stop_reason", None) == "aborted"
        ):
            pre = self._pre_prompt_leaf_id
            # `branch(id)` requires a known id; reset_leaf() handles the
            # None case (turn started on an empty session). Direct-assign
            # mirrors Pi's own internal rewind style (see sdk.py's resume
            # path which also treats _leaf_id as writable).
            if pre is None:
                self._session_manager.reset_leaf()
            else:
                self._session_manager._leaf_id = pre
            # Rebuild agent context from the rewound leaf. agent._state.messages
            # still has every MessageEndEvent from this turn appended, so we
            # must replace it wholesale — the same pattern create_agent_session
            # uses to prime a resumed session (see sdk.py:270 /
            # _handle_agent_event paths).
            context = self._session_manager.build_session_context()
            self._agent.replace_messages(context.messages)
        self._pre_prompt_leaf_id = None

    # =========================================================================
    # Model management
    # =========================================================================

    async def set_model(self, model: Model) -> None:
        """Set model directly."""
        self._agent.set_model(model)
        self._session_manager.append_model_change(model.provider, model.id)

    def set_thinking_level(self, level: ThinkingLevel) -> None:
        """Set the thinking level, clamping to model capabilities."""
        available = self._get_available_thinking_levels()
        effective = level if level in available else self._clamp_thinking_level(level, available)
        if effective != self.thinking_level:
            self._agent.set_thinking_level(effective)
            self._session_manager.append_thinking_level_change(effective)

    def _get_available_thinking_levels(self) -> list[ThinkingLevel]:
        """Return available thinking levels for the current model."""
        if self.model is None or not getattr(self.model, "reasoning", False):
            return ["off"]
        # Check if model supports xhigh (simplified: check model id for known patterns)
        return THINKING_LEVELS_WITH_XHIGH if self._supports_xhigh() else THINKING_LEVELS

    def _supports_xhigh(self) -> bool:
        """Check if current model supports xhigh thinking."""
        if self.model is None:
            return False
        model_id = getattr(self.model, "id", "").lower()
        return "claude-3-7" in model_id or "claude-opus-4" in model_id

    def _clamp_thinking_level(self, level: ThinkingLevel, available: list[ThinkingLevel]) -> ThinkingLevel:
        """Clamp a thinking level to the nearest available level."""
        ordered = THINKING_LEVELS_WITH_XHIGH
        available_set = set(available)
        requested_idx = ordered.index(level) if level in ordered else -1

        if requested_idx == -1:
            return available[0] if available else "off"

        for i in range(requested_idx, len(ordered)):
            if ordered[i] in available_set:
                return ordered[i]
        for i in range(requested_idx - 1, -1, -1):
            if ordered[i] in available_set:
                return ordered[i]
        return available[0] if available else "off"

    # =========================================================================
    # Session management
    # =========================================================================

    async def new_session(self, **kwargs: Any) -> None:
        """Start a new session. Clears all messages and starts fresh."""
        parent_session: str | None = kwargs.get("parent_session")
        self._disconnect_from_agent()
        await self.abort()
        self._agent.reset()

        self._session_manager.new_session(NewSessionOptions(parent_session=parent_session))
        self._agent.session_id = self._session_manager.get_session_id()
        self._steering_messages.clear()
        self._follow_up_messages.clear()
        self._pending_next_turn_messages.clear()
        self._session_manager.append_thinking_level_change(self.thinking_level)
        self._reconnect_to_agent()

    # =========================================================================
    # Compaction

    # =========================================================================

    async def compact(self, custom_instructions: str | None = None) -> Any:
        """Manually compact the session context.

        Disconnect from agent, run compaction pipeline, update session
        and agent state with the compacted context.

        Returns:
            CompactionResult with summary, first_kept_entry_id, tokens_before, details.
        """
        from pi.coding_agent.core.compaction.compaction import (  # noqa: I001
            CompactionResult,
            CompactionSettings,
            compact as run_compact,
            prepare_compaction,
        )
        from pi.coding_agent.core.session_manager import CompactionEntry

        self._disconnect_from_agent()
        await self.abort()
        self._compaction_abort_event = asyncio.Event()

        try:
            if not self.model:
                raise RuntimeError("No model selected")

            api_key = await self._model_registry.get_api_key(self.model)
            if not api_key:
                raise RuntimeError(f"No API key for {self.model.provider}")

            path_entries = self._session_manager.get_branch()
            raw_settings = self._settings_manager.get_compaction_settings()
            settings = CompactionSettings(**raw_settings) if isinstance(raw_settings, dict) else raw_settings

            preparation = prepare_compaction(path_entries, settings)
            if not preparation:
                last_entry = path_entries[-1] if path_entries else None
                if last_entry is not None and isinstance(last_entry, CompactionEntry):
                    raise RuntimeError("Already compacted")
                raise RuntimeError("Nothing to compact (session too small)")

            extension_compaction: CompactionResult | None = None
            from_extension = False

            runner = self._extension_runner_ref.get("current") if self._extension_runner_ref else None
            if runner is not None and runner.has_handlers("session_before_compact"):
                ext_result = await runner.emit(
                    {
                        "type": "session_before_compact",
                        "preparation": preparation,
                        "branch_entries": path_entries,
                        "custom_instructions": custom_instructions,
                        "signal": self._compaction_abort_event,
                    }
                )

                if ext_result is not None and getattr(ext_result, "cancel", False):
                    raise RuntimeError("Compaction cancelled")

                if ext_result is not None and getattr(ext_result, "compaction", None) is not None:
                    extension_compaction = ext_result.compaction
                    from_extension = True

            if extension_compaction is not None:
                summary = extension_compaction.summary
                first_kept_entry_id = extension_compaction.first_kept_entry_id
                tokens_before = extension_compaction.tokens_before
                details = extension_compaction.details
            else:
                result = await run_compact(
                    preparation,
                    self.model,
                    api_key,
                    custom_instructions,
                    self._compaction_abort_event,
                )
                summary = result.summary
                first_kept_entry_id = result.first_kept_entry_id
                tokens_before = result.tokens_before
                details = result.details

            if self._compaction_abort_event.is_set():
                raise RuntimeError("Compaction cancelled")

            self._session_manager.append_compaction(
                summary,
                first_kept_entry_id,
                tokens_before,
                details,
                from_extension,
            )
            new_entries = self._session_manager.get_entries()
            session_context = self._session_manager.build_session_context()
            self._agent.replace_messages(session_context.messages)

            # Emit session_compact extension event
            saved = next(
                (e for e in new_entries if isinstance(e, CompactionEntry) and e.summary == summary),
                None,
            )
            if runner is not None and saved is not None:
                with contextlib.suppress(Exception):
                    await runner.emit(
                        {
                            "type": "session_compact",
                            "compaction_entry": saved,
                            "from_extension": from_extension,
                        }
                    )

            return CompactionResult(
                summary=summary,
                first_kept_entry_id=first_kept_entry_id,
                tokens_before=tokens_before,
                details=details,
            )
        finally:
            self._compaction_abort_event = None
            self._reconnect_to_agent()

    async def _check_compaction(
        self,
        assistant_message: AssistantMessage,
        skip_aborted_check: bool = True,
    ) -> None:
        """Check if auto-compaction is needed after an agent turn.

        Two cases:
        1. Overflow: LLM returned context overflow error -> remove error msg, compact, auto-retry
        2. Threshold: Context over threshold -> compact, NO auto-retry

        Args:
            assistant_message: The assistant message to check.
            skip_aborted_check: If False, include aborted messages (for pre-prompt check).
        """
        from pi.ai.utils.overflow import is_context_overflow
        from pi.coding_agent.core.compaction.compaction import (
            CompactionSettings,
            calculate_context_tokens,
            should_compact,
        )
        from pi.coding_agent.core.session_manager import get_latest_compaction_entry

        raw_settings = self._settings_manager.get_compaction_settings()
        settings = CompactionSettings(**raw_settings) if isinstance(raw_settings, dict) else raw_settings
        if not settings.enabled:
            return

        # Skip if message was aborted (user cancelled) - unless skip_aborted_check is False
        if skip_aborted_check and getattr(assistant_message, "stop_reason", None) == "aborted":
            return

        context_window = getattr(self.model, "context_window", 0) or 0

        # Skip overflow check if the message came from a different model
        same_model = (
            self.model is not None
            and getattr(assistant_message, "provider", "") == self.model.provider
            and getattr(assistant_message, "model", "") == self.model.id
        )

        # Skip overflow check if the error is from before a compaction in the current path
        compaction_entry = get_latest_compaction_entry(self._session_manager.get_branch())
        error_is_from_before_compaction = (
            compaction_entry is not None and getattr(assistant_message, "timestamp", 0) < compaction_entry.timestamp
        )

        # Case 1: Overflow - LLM returned context overflow error
        is_overflow = is_context_overflow(assistant_message, context_window)
        if same_model and not error_is_from_before_compaction and is_overflow:
            messages = self._agent.state.messages
            if messages and messages[-1].role == "assistant":
                self._agent.replace_messages(messages[:-1])
            await self._run_auto_compaction("overflow", True)
            return

        # Case 2: Threshold - turn succeeded but context is getting large
        if getattr(assistant_message, "stop_reason", None) == "error":
            return

        context_tokens = calculate_context_tokens(assistant_message.usage)
        if should_compact(context_tokens, context_window, settings):
            await self._run_auto_compaction("threshold", False)

    async def _run_auto_compaction(
        self,
        reason: Literal["overflow", "threshold"],
        will_retry: bool,
    ) -> None:
        """Run auto-compaction with events."""
        from pi.coding_agent.core.compaction.compaction import (  # noqa: I001
            CompactionResult,
            CompactionSettings,
            compact as run_compact,
            prepare_compaction,
        )
        from pi.coding_agent.core.session_manager import CompactionEntry

        raw_settings = self._settings_manager.get_compaction_settings()
        settings = CompactionSettings(**raw_settings) if isinstance(raw_settings, dict) else raw_settings

        self._emit(AutoCompactionStartEvent(reason=reason))
        self._auto_compaction_abort_event = asyncio.Event()

        try:
            if not self.model:
                self._emit(AutoCompactionEndEvent(result=None, aborted=False, will_retry=False))
                return

            api_key = await self._model_registry.get_api_key(self.model)
            if not api_key:
                self._emit(AutoCompactionEndEvent(result=None, aborted=False, will_retry=False))
                return

            path_entries = self._session_manager.get_branch()

            preparation = prepare_compaction(path_entries, settings)
            if not preparation:
                self._emit(AutoCompactionEndEvent(result=None, aborted=False, will_retry=False))
                return

            extension_compaction: CompactionResult | None = None
            from_extension = False

            runner = self._extension_runner_ref.get("current") if self._extension_runner_ref else None
            if runner is not None and runner.has_handlers("session_before_compact"):
                ext_result = await runner.emit(
                    {
                        "type": "session_before_compact",
                        "preparation": preparation,
                        "branch_entries": path_entries,
                        "custom_instructions": None,
                        "signal": self._auto_compaction_abort_event,
                    }
                )

                if ext_result is not None and getattr(ext_result, "cancel", False):
                    self._emit(AutoCompactionEndEvent(result=None, aborted=True, will_retry=False))
                    return

                if ext_result is not None and getattr(ext_result, "compaction", None) is not None:
                    extension_compaction = ext_result.compaction
                    from_extension = True

            if extension_compaction is not None:
                summary = extension_compaction.summary
                first_kept_entry_id = extension_compaction.first_kept_entry_id
                tokens_before = extension_compaction.tokens_before
                details = extension_compaction.details
            else:
                compact_result = await run_compact(
                    preparation,
                    self.model,
                    api_key,
                    None,
                    self._auto_compaction_abort_event,
                )
                summary = compact_result.summary
                first_kept_entry_id = compact_result.first_kept_entry_id
                tokens_before = compact_result.tokens_before
                details = compact_result.details

            if self._auto_compaction_abort_event.is_set():
                self._emit(AutoCompactionEndEvent(result=None, aborted=True, will_retry=False))
                return

            self._session_manager.append_compaction(
                summary,
                first_kept_entry_id,
                tokens_before,
                details,
                from_extension,
            )
            new_entries = self._session_manager.get_entries()
            session_context = self._session_manager.build_session_context()
            self._agent.replace_messages(session_context.messages)

            # Emit session_compact extension event
            saved = next(
                (e for e in new_entries if isinstance(e, CompactionEntry) and e.summary == summary),
                None,
            )
            if runner is not None and saved is not None:
                with contextlib.suppress(Exception):
                    await runner.emit(
                        {
                            "type": "session_compact",
                            "compaction_entry": saved,
                            "from_extension": from_extension,
                        }
                    )

            result = CompactionResult(
                summary=summary,
                first_kept_entry_id=first_kept_entry_id,
                tokens_before=tokens_before,
                details=details,
            )
            self._emit(AutoCompactionEndEvent(result=result, aborted=False, will_retry=will_retry))

            if will_retry:
                messages = self._agent.state.messages
                last_msg = messages[-1] if messages else None
                if (
                    last_msg is not None
                    and last_msg.role == "assistant"
                    and getattr(last_msg, "stop_reason", "") == "error"
                ):
                    self._agent.replace_messages(messages[:-1])

                async def _retry() -> None:
                    await asyncio.sleep(0.1)
                    with contextlib.suppress(Exception):
                        await self._agent.continue_()

                self._compaction_task = asyncio.ensure_future(_retry())
            elif hasattr(self._agent, "has_queued_messages") and self._agent.has_queued_messages():

                async def _continue_queued() -> None:
                    await asyncio.sleep(0.1)
                    with contextlib.suppress(Exception):
                        await self._agent.continue_()

                self._compaction_task = asyncio.ensure_future(_continue_queued())

        except Exception as exc:
            error_message = str(exc) if str(exc) else "compaction failed"
            msg = (
                f"Context overflow recovery failed: {error_message}"
                if reason == "overflow"
                else f"Auto-compaction failed: {error_message}"
            )
            self._emit(
                AutoCompactionEndEvent(
                    result=None,
                    aborted=False,
                    will_retry=False,
                    error_message=msg,
                )
            )
        finally:
            self._auto_compaction_abort_event = None

    # =========================================================================
    # Bash execution
    # =========================================================================

    async def execute_bash(
        self,
        command: str,
        on_chunk: Callable[[str], None] | None = None,
        exclude_from_context: bool | None = None,
    ) -> BashResult:
        """Execute a bash command and record the result in the session.

        Args:
            command: The bash command to execute.
            on_chunk: Optional streaming callback for output chunks.
            exclude_from_context: If True, output won't be sent to the LLM.

        Returns:
            BashResult with output, exit code, and status.
        """
        self._bash_abort_event = asyncio.Event()

        options = BashExecutorOptions(
            on_chunk=on_chunk,
            signal=self._bash_abort_event,
        )

        try:
            result = await execute_bash(command, options)
            self._record_bash_result(command, result, exclude_from_context=exclude_from_context)
            return result
        finally:
            self._bash_abort_event = None

    def _record_bash_result(
        self,
        command: str,
        result: BashResult,
        exclude_from_context: bool | None = None,
    ) -> None:
        """Record a bash execution result in the session and agent state."""
        bash_message = BashExecutionMessage(
            command=command,
            stdout=result.output,
            stderr="",
            exit_code=result.exit_code,
            cancelled=result.cancelled,
            truncated=result.truncated,
            full_output_path=result.full_output_path,
            exclude_from_context=exclude_from_context,
        )

        if self.is_streaming:
            # Defer until after agent turn to preserve message ordering
            self._pending_bash_messages.append(bash_message)
        else:
            self._agent.append_message(bash_message)
            self._session_manager.append_message(bash_message)

    def abort_bash(self) -> None:
        """Cancel the currently running bash command."""
        if self._bash_abort_event is not None:
            self._bash_abort_event.set()

    def _flush_pending_bash_messages(self) -> None:
        """Flush deferred bash messages to agent state and session."""
        for bash_message in self._pending_bash_messages:
            self._agent.append_message(bash_message)
            self._session_manager.append_message(bash_message)
        self._pending_bash_messages.clear()

    # =========================================================================
    # Queue management
    # =========================================================================

    async def _queue_steer(self, text: str, images: list[ImageContent] | None = None) -> None:
        """Queue a steering message to interrupt the agent mid-run."""
        self._steering_messages.append(text)
        content: list[TextContent | ImageContent] = [TextContent(text=text)]
        if images:
            content.extend(images)
        self._agent.steer(UserMessage(content=content))

    async def _queue_follow_up(self, text: str, images: list[ImageContent] | None = None) -> None:
        """Queue a follow-up message to be processed after the agent finishes."""
        self._follow_up_messages.append(text)
        content: list[TextContent | ImageContent] = [TextContent(text=text)]
        if images:
            content.extend(images)
        self._agent.follow_up(UserMessage(content=content))

    def clear_queue(self) -> dict[str, list[str]]:
        """Clear all queued messages. Returns the cleared queues."""
        steering = list(self._steering_messages)
        follow_up = list(self._follow_up_messages)
        self._steering_messages.clear()
        self._follow_up_messages.clear()
        if hasattr(self._agent, "clear_all_queues"):
            self._agent.clear_all_queues()
        return {"steering": steering, "follow_up": follow_up}

    # =========================================================================
    # Auto-retry
    # =========================================================================

    def _is_retryable_error(self, message: AssistantMessage) -> bool:
        """Check if an error is retryable (overloaded, rate limit, server errors)."""
        stop_reason = getattr(message, "stop_reason", "")
        error_msg = getattr(message, "error_message", "") or ""
        if stop_reason != "error" or not error_msg:
            return False
        return bool(_RETRYABLE_ERROR_PATTERN.search(error_msg))

    def _abort_retry(self) -> None:
        """Cancel in-progress retry."""
        if self._retry_abort_event is not None:
            self._retry_abort_event.set()
        if self._retry_future is not None:
            self._retry_future.cancel()
            self._retry_future = None

    def abort_compaction(self) -> None:
        """Cancel in-progress compaction."""
        if self._compaction_abort_event is not None:
            self._compaction_abort_event.set()
        if self._auto_compaction_abort_event is not None:
            self._auto_compaction_abort_event.set()

    # =========================================================================
    # Session stats
    # =========================================================================

    def get_session_stats(self) -> SessionStats:
        """Compute session statistics."""
        messages = self.messages
        user_count = sum(1 for m in messages if getattr(m, "role", "") == "user")
        assistant_count = sum(1 for m in messages if getattr(m, "role", "") == "assistant")
        tool_result_count = sum(1 for m in messages if getattr(m, "role", "") == "toolResult")

        # Count tool calls in assistant messages
        tool_call_count = 0
        total_tokens: dict[str, int] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0}
        total_cost = 0.0

        for m in messages:
            if getattr(m, "role", "") == "assistant":
                content = getattr(m, "content", [])
                if isinstance(content, list):
                    from pi.ai.types import ToolCall

                    tool_call_count += sum(1 for b in content if isinstance(b, ToolCall))
                usage = getattr(m, "usage", None)
                if usage is not None:
                    total_tokens["input"] += getattr(usage, "input", 0)
                    total_tokens["output"] += getattr(usage, "output", 0)
                    total_tokens["cache_read"] += getattr(usage, "cache_read", 0)
                    total_tokens["cache_write"] += getattr(usage, "cache_write", 0)
                    total_tokens["total"] += getattr(usage, "total_tokens", 0)
                    cost = getattr(usage, "cost", None)
                    if cost is not None:
                        total_cost += getattr(cost, "total", 0.0)

        return SessionStats(
            session_file=self.session_file,
            session_id=self.session_id,
            user_messages=user_count,
            assistant_messages=assistant_count,
            tool_calls=tool_call_count,
            tool_results=tool_result_count,
            total_messages=len(messages),
            tokens=total_tokens,
            cost=total_cost,
        )
