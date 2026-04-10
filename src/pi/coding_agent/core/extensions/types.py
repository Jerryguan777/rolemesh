"""Extension system types.

Port of packages/coding-agent/src/core/extensions/types.ts.

Extensions can:
- Subscribe to agent lifecycle events
- Register LLM-callable tools
- Register commands, keyboard shortcuts, and CLI flags
- Interact with the user via UI primitives
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# TUI types are not yet ported - typed as Any
TUI = Any
Component = Any
EditorComponent = Any
EditorTheme = Any
KeyId = str
OverlayHandle = Any
OverlayOptions = Any
Theme = Any

# Input source (TS: "interactive" | "rpc" | "extension")
InputSource = Literal["interactive", "rpc", "extension"]

# Model select source (TS: "set" | "cycle" | "restore")
ModelSelectSource = Literal["set", "cycle", "restore"]

# Widget placement (TS: "aboveEditor" | "belowEditor")
WidgetPlacement = Literal["aboveEditor", "belowEditor"]

# Handler type aliases (from extensions/types.ts)
# These are Callable type aliases used by ExtensionActions/ExtensionRuntime.
SendMessageHandler = Callable[[Any, Any], None]
SendUserMessageHandler = Callable[[str | list[Any], Any], None]
AppendEntryHandler = Callable[[str, Any], None]
SetSessionNameHandler = Callable[[str], None]
GetSessionNameHandler = Callable[[], str | None]
GetActiveToolsHandler = Callable[[], list[str]]
GetAllToolsHandler = Callable[[], list["ToolInfo"]]
GetCommandsHandler = Callable[[], list[Any]]  # list[SlashCommandInfo]
SetActiveToolsHandler = Callable[[list[str]], None]
SetModelHandler = Callable[[Any], Awaitable[bool]]  # Model -> bool
GetThinkingLevelHandler = Callable[[], str]  # ThinkingLevel
SetThinkingLevelHandler = Callable[[str], None]  # ThinkingLevel
SetLabelHandler = Callable[[str, str | None], None]

# Terminal input handler (from extensions/types.ts)
TerminalInputHandler = Callable[[str], dict[str, Any] | None]

# Extension handler generic type (from extensions/types.ts)
# ExtensionHandler<E, R> = (event: E, ctx: ExtensionContext) -> R | None
ExtensionHandler = Callable[[Any, "ExtensionContext"], Awaitable[Any] | Any]


# ============================================================================
# UI Dialog/Widget Options
# ============================================================================


@dataclass
class ExtensionUIDialogOptions:
    """Options for extension UI dialogs."""

    signal: Any = None  # asyncio.Event or None
    timeout: int | None = None


@dataclass
class ExtensionWidgetOptions:
    """Options for extension widgets."""

    placement: WidgetPlacement = "aboveEditor"


@dataclass
class ExtensionUIContext:
    """UI context for extensions to request interactive UI.

    Each mode (interactive, RPC, print) provides its own implementation.
    """

    select: Any = None  # Callable
    confirm: Any = None  # Callable
    input: Any = None  # Callable
    notify: Any = None  # Callable
    on_terminal_input: Any = None  # Callable
    set_status: Any = None  # Callable
    set_working_message: Any = None  # Callable
    set_widget: Any = None  # Callable
    set_footer: Any = None  # Callable
    set_header: Any = None  # Callable
    set_title: Any = None  # Callable
    custom: Any = None  # Callable
    paste_to_editor: Any = None  # Callable
    set_editor_text: Any = None  # Callable
    get_editor_text: Any = None  # Callable
    editor: Any = None  # Callable
    set_editor_component: Any = None  # Callable
    theme: Any = None  # Theme
    get_all_themes: Any = None  # Callable
    get_theme: Any = None  # Callable
    set_theme: Any = None  # Callable
    get_tools_expanded: Any = None  # Callable
    set_tools_expanded: Any = None  # Callable


# ============================================================================
# Message Rendering
# ============================================================================


@dataclass
class MessageRenderOptions:
    """Options for rendering a custom message."""

    expanded: bool = False


# MessageRenderer: (message, options, theme) -> Component | None
MessageRenderer = Callable[[Any, MessageRenderOptions, Any], Any]


# ============================================================================
# Tool Render Options
# ============================================================================


@dataclass
class ToolRenderResultOptions:
    """Rendering options for tool results."""

    expanded: bool = False
    is_partial: bool = False


# ============================================================================
# Tool Info
# ============================================================================


@dataclass
class ToolInfo:
    """Information about a registered tool."""

    name: str
    active: bool
    label: str
    description: str


# ============================================================================
# Event types
# ============================================================================


@dataclass
class SessionStartEvent:
    type: Literal["session_start"] = "session_start"


@dataclass
class SessionShutdownEvent:
    type: Literal["session_shutdown"] = "session_shutdown"


@dataclass
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass
class AgentEndEvent:
    type: Literal["agent_end"] = "agent_end"


@dataclass
class ToolExecutionStartEvent:
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str = ""
    tool_name: str = ""


@dataclass
class ToolExecutionEndEvent:
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str = ""
    tool_name: str = ""
    is_error: bool = False


@dataclass
class ToolExecutionUpdateEvent:
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str = ""
    tool_name: str = ""


@dataclass
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass
class TurnEndEvent:
    type: Literal["turn_end"] = "turn_end"


@dataclass
class MessageStartEvent:
    type: Literal["message_start"] = "message_start"


@dataclass
class MessageUpdateEvent:
    type: Literal["message_update"] = "message_update"


@dataclass
class MessageEndEvent:
    type: Literal["message_end"] = "message_end"


@dataclass
class ToolCallEvent:
    type: Literal["tool_call"] = "tool_call"
    tool_name: str = ""
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


# Per-tool typed call events


@dataclass
class BashToolCallEvent:
    """Fired before the bash tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["bash"] = "bash"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReadToolCallEvent:
    """Fired before the read tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["read"] = "read"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class EditToolCallEvent:
    """Fired before the edit tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["edit"] = "edit"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class WriteToolCallEvent:
    """Fired before the write tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["write"] = "write"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class GrepToolCallEvent:
    """Fired before the grep tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["grep"] = "grep"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class FindToolCallEvent:
    """Fired before the find tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["find"] = "find"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class LsToolCallEvent:
    """Fired before the ls tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: Literal["ls"] = "ls"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class CustomToolCallEvent:
    """Fired before a custom (extension) tool executes."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: str = ""
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallEventResult:
    block: bool = False
    reason: str | None = None


@dataclass
class ToolResultEvent:
    type: Literal["tool_result"] = "tool_result"
    tool_name: str = ""
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


# Per-tool typed result events


@dataclass
class BashToolResultEvent:
    """Fired after the bash tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["bash"] = "bash"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class ReadToolResultEvent:
    """Fired after the read tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["read"] = "read"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class EditToolResultEvent:
    """Fired after the edit tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["edit"] = "edit"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class WriteToolResultEvent:
    """Fired after the write tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["write"] = "write"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: None = None
    is_error: bool = False


@dataclass
class GrepToolResultEvent:
    """Fired after the grep tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["grep"] = "grep"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class FindToolResultEvent:
    """Fired after the find tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["find"] = "find"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class LsToolResultEvent:
    """Fired after the ls tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: Literal["ls"] = "ls"
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class CustomToolResultEvent:
    """Fired after a custom (extension) tool executes."""

    type: Literal["tool_result"] = "tool_result"
    tool_name: str = ""
    tool_call_id: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    content: list[Any] = field(default_factory=list)
    details: Any = None
    is_error: bool = False


@dataclass
class ToolResultEventResult:
    content: list[Any] | None = None
    details: Any = None
    is_error: bool | None = None


@dataclass
class ContextEvent:
    type: Literal["context"] = "context"
    messages: list[Any] = field(default_factory=list)  # AgentMessage


@dataclass
class ContextEventResult:
    messages: list[Any] | None = None


@dataclass
class SessionBeforeCompactEvent:
    type: Literal["session_before_compact"] = "session_before_compact"
    preparation: Any = None  # CompactionPreparation


@dataclass
class SessionBeforeCompactResult:
    cancel: bool = False


@dataclass
class SessionBeforeForkEvent:
    type: Literal["session_before_fork"] = "session_before_fork"


@dataclass
class SessionBeforeForkResult:
    cancel: bool = False


@dataclass
class SessionBeforeSwitchEvent:
    type: Literal["session_before_switch"] = "session_before_switch"


@dataclass
class SessionBeforeSwitchResult:
    cancel: bool = False


@dataclass
class SessionBeforeTreeEvent:
    type: Literal["session_before_tree"] = "session_before_tree"


@dataclass
class SessionBeforeTreeResult:
    cancel: bool = False


@dataclass
class SessionCompactEvent:
    type: Literal["session_compact"] = "session_compact"


@dataclass
class SessionForkEvent:
    type: Literal["session_fork"] = "session_fork"


@dataclass
class SessionSwitchEvent:
    type: Literal["session_switch"] = "session_switch"


@dataclass
class SessionEvent:
    type: Literal["session_event"] = "session_event"


@dataclass
class SessionTreeEvent:
    type: Literal["session_tree"] = "session_tree"


@dataclass
class BeforeAgentStartEvent:
    type: Literal["before_agent_start"] = "before_agent_start"
    prompt: str = ""
    images: list[Any] | None = None  # ImageContent
    system_prompt: str = ""


@dataclass
class BeforeAgentStartEventResult:
    message: Any | None = None
    system_prompt: str | None = None


@dataclass
class InputEvent:
    type: Literal["input"] = "input"
    text: str = ""
    images: list[Any] | None = None
    source: str = "user"


@dataclass
class InputEventResult:
    action: str = "continue"  # "continue" | "handled" | "transform"
    text: str | None = None
    images: list[Any] | None = None


@dataclass
class ResourcesDiscoverEvent:
    type: Literal["resources_discover"] = "resources_discover"
    cwd: str = ""
    reason: str = ""  # "startup" | "reload" | "settings_change"


@dataclass
class ResourcesDiscoverResult:
    skill_paths: list[str] | None = None
    prompt_paths: list[str] | None = None
    theme_paths: list[str] | None = None


@dataclass
class UserBashEvent:
    type: Literal["user_bash"] = "user_bash"
    command: str = ""


@dataclass
class UserBashEventResult:
    allow: bool = False


@dataclass
class ModelSelectEvent:
    type: Literal["model_select"] = "model_select"
    model: Any = None  # Model


# ============================================================================
# Extension structure types
# ============================================================================


@dataclass
class ExtensionError:
    """An error that occurred in an extension handler."""

    extension_path: str
    event: str
    error: str
    stack: str | None = None


@dataclass
class ExtensionFlag:
    """A CLI flag registered by an extension."""

    name: str
    extension_path: str
    description: str | None = None
    type: str = "boolean"  # "boolean" | "string"
    default: bool | str | None = None


@dataclass
class ExtensionShortcut:
    """A keyboard shortcut registered by an extension."""

    shortcut: str  # KeyId
    extension_path: str
    description: str | None = None
    handler: Any = None  # Callable


@dataclass
class ToolDefinition:
    """Definition of a tool that can be registered by extensions."""

    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    execute: Any  # Callable


@dataclass
class RegisteredTool:
    """A tool registered by an extension."""

    definition: ToolDefinition
    extension_path: str


@dataclass
class RegisteredCommand:
    """A command registered by an extension."""

    name: str
    description: str | None = None
    handler: Any = None  # Callable


@dataclass
class ProviderConfig:
    """Provider configuration for extension-registered providers."""

    base_url: str | None = None
    api_key: str | None = None
    api: str | None = None
    models: list[dict[str, Any]] | None = None


@dataclass
class ProviderModelConfig:
    """Model configuration within a provider."""

    id: str
    name: str
    api: str | None = None


@dataclass
class LoadExtensionsResult:
    """Result of loading extensions."""

    extensions: list[Extension]
    errors: list[dict[str, str]]  # {path, error}
    runtime: ExtensionRuntime


@dataclass
class Extension:
    """A loaded extension with its registered handlers and tools."""

    path: str
    resolved_path: str
    handlers: dict[str, list[Any]]  # event_type -> list of handlers
    tools: dict[str, RegisteredTool]
    message_renderers: dict[str, Any]
    commands: dict[str, RegisteredCommand]
    flags: dict[str, ExtensionFlag]
    shortcuts: dict[str, ExtensionShortcut]


# ============================================================================
# Context usage
# ============================================================================


@dataclass
class ContextUsage:
    """Current context token usage."""

    context_tokens: int
    context_window: int
    usage_percent: float


@dataclass
class CompactOptions:
    """Options for compaction."""

    custom_instructions: str | None = None


# ============================================================================
# Tree Preparation
# ============================================================================


@dataclass
class TreePreparation:
    """Preparation data for tree navigation."""

    target_id: str = ""
    old_leaf_id: str | None = None
    common_ancestor_id: str | None = None
    entries_to_summarize: list[Any] = field(default_factory=list)  # SessionEntry[]
    user_wants_summary: bool = False
    custom_instructions: str | None = None
    replace_instructions: bool = False
    label: str | None = None


# ============================================================================
# Extension Runtime State
# ============================================================================


@dataclass
class ExtensionRuntimeState:
    """Shared state created by loader, used during registration and runtime."""

    flag_values: dict[str, bool | str] = field(default_factory=dict)
    pending_provider_registrations: list[dict[str, Any]] = field(default_factory=list)


# ============================================================================
# ExtensionContext
# ============================================================================


@dataclass
class ExtensionContext:
    """Context passed to extension handlers."""

    cwd: str
    has_ui: bool = False
    ui: Any = None  # ExtensionUIContext
    session_manager: Any = None  # ReadonlySessionManager
    model_registry: Any = None  # ModelRegistry
    model: Any = None  # Model | None
    is_idle: Any = None  # Callable[[], bool]
    abort: Any = None  # Callable[[], None]
    has_pending_messages: Any = None  # Callable[[], bool]
    shutdown: Any = None  # Callable[[], None]
    get_context_usage: Any = None  # Callable[[], ContextUsage | None]
    compact: Any = None  # Callable[[CompactOptions | None], None]
    get_system_prompt: Any = None  # Callable[[], str]


# ============================================================================
# ExtensionActions - actions bound by core
# ============================================================================


@dataclass
class ExtensionActions:
    """Actions provided by the core to the extension runtime."""

    send_message: Any = None
    send_user_message: Any = None
    append_entry: Any = None
    set_session_name: Any = None
    get_session_name: Any = None
    set_label: Any = None
    get_active_tools: Any = None
    get_all_tools: Any = None
    set_active_tools: Any = None
    get_commands: Any = None
    set_model: Any = None
    get_thinking_level: Any = None
    set_thinking_level: Any = None


@dataclass
class ExtensionContextActions:
    """Context-level actions for extensions."""

    get_model: Any = None
    is_idle: Any = None
    wait_for_idle: Any = None
    abort: Any = None
    has_pending_messages: Any = None
    get_context_usage: Any = None
    compact: Any = None
    get_system_prompt: Any = None


@dataclass
class ExtensionCommandContext:
    """Command context with additional navigation capabilities."""

    cwd: str = ""
    has_ui: bool = False
    ui: Any = None
    session_manager: Any = None
    model_registry: Any = None
    model: Any = None
    is_idle: Any = None
    abort: Any = None
    has_pending_messages: Any = None
    shutdown: Any = None
    get_context_usage: Any = None
    compact: Any = None
    get_system_prompt: Any = None
    wait_for_idle: Any = None
    new_session: Any = None
    fork: Any = None
    navigate_tree: Any = None
    switch_session: Any = None
    reload: Any = None


@dataclass
class ExtensionCommandContextActions:
    """Actions available in command context."""

    wait_for_idle: Any = None
    new_session: Any = None
    fork: Any = None
    navigate_tree: Any = None
    switch_session: Any = None
    reload: Any = None


# ============================================================================
# ExtensionRuntime - shared mutable object between all extensions
# ============================================================================


class ExtensionRuntime:
    """Shared mutable runtime state for all extensions.

    Runner.bind_core() replaces stub methods with real implementations.
    """

    def __init__(self) -> None:
        self.flag_values: dict[str, bool | str] = {}
        self.pending_provider_registrations: list[dict[str, Any]] = []

        def _not_initialized(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                "Extension runtime not initialized. Action methods cannot be called during extension loading."
            )

        self.send_message: Any = _not_initialized
        self.send_user_message: Any = _not_initialized
        self.append_entry: Any = _not_initialized
        self.set_session_name: Any = _not_initialized
        self.get_session_name: Any = _not_initialized
        self.set_label: Any = _not_initialized
        self.get_active_tools: Any = _not_initialized
        self.get_all_tools: Any = _not_initialized
        self.set_active_tools: Any = _not_initialized
        self.get_commands: Any = _not_initialized
        self.set_model: Any = _not_initialized
        self.get_thinking_level: Any = _not_initialized
        self.set_thinking_level: Any = _not_initialized


# ============================================================================
# ExtensionAPI - what extensions receive when registering
# ============================================================================

# ExtensionFactory type: an async callable that receives an ExtensionAPI
ExtensionFactory = Callable[["ExtensionAPI"], Awaitable[None]]

# Union of all extension events
ExtensionEvent = (
    SessionStartEvent
    | SessionShutdownEvent
    | AgentStartEvent
    | AgentEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionEndEvent
    | ToolExecutionUpdateEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolCallEvent
    | ToolResultEvent
    | ContextEvent
    | SessionBeforeCompactEvent
    | SessionBeforeForkEvent
    | SessionBeforeSwitchEvent
    | SessionBeforeTreeEvent
    | SessionCompactEvent
    | SessionForkEvent
    | SessionSwitchEvent
    | SessionEvent
    | SessionTreeEvent
    | BeforeAgentStartEvent
    | InputEvent
    | ResourcesDiscoverEvent
    | UserBashEvent
    | ModelSelectEvent
)


class ExtensionAPI:
    """API provided to extensions during initialization.

    Registration methods write to the extension object.
    Action methods delegate to the shared runtime.
    """

    def __init__(
        self,
        extension: Extension,
        runtime: ExtensionRuntime,
        cwd: str,
    ) -> None:
        self._extension = extension
        self._runtime = runtime
        self._cwd = cwd

    def on(self, event: str, handler: Any) -> None:
        """Register an event handler."""
        handlers = self._extension.handlers.get(event, [])
        handlers.append(handler)
        self._extension.handlers[event] = handlers

    def register_tool(self, tool: ToolDefinition) -> None:
        """Register a tool."""
        self._extension.tools[tool.name] = RegisteredTool(
            definition=tool,
            extension_path=self._extension.path,
        )

    def register_command(self, name: str, options: dict[str, Any]) -> None:
        """Register a slash command."""
        self._extension.commands[name] = RegisteredCommand(
            name=name,
            description=options.get("description"),
            handler=options.get("handler"),
        )

    def register_shortcut(self, shortcut: str, options: dict[str, Any]) -> None:
        """Register a keyboard shortcut."""
        self._extension.shortcuts[shortcut] = ExtensionShortcut(
            shortcut=shortcut,
            extension_path=self._extension.path,
            description=options.get("description"),
            handler=options.get("handler"),
        )

    def register_flag(self, name: str, options: dict[str, Any]) -> None:
        """Register a CLI flag."""
        flag = ExtensionFlag(
            name=name,
            extension_path=self._extension.path,
            description=options.get("description"),
            type=options.get("type", "boolean"),
            default=options.get("default"),
        )
        self._extension.flags[name] = flag
        if flag.default is not None:
            self._runtime.flag_values[name] = flag.default

    def register_message_renderer(self, custom_type: str, renderer: Any) -> None:
        """Register a custom message renderer."""
        self._extension.message_renderers[custom_type] = renderer

    def get_flag(self, name: str) -> bool | str | None:
        """Get a flag value registered by this extension."""
        if name not in self._extension.flags:
            return None
        return self._runtime.flag_values.get(name)

    def send_message(self, message: Any, options: Any = None) -> None:
        """Send a message to the agent."""
        self._runtime.send_message(message, options)

    def send_user_message(self, content: Any, options: Any = None) -> None:
        """Send a user message."""
        self._runtime.send_user_message(content, options)

    def append_entry(self, custom_type: str, data: Any = None) -> None:
        """Append a custom session entry."""
        self._runtime.append_entry(custom_type, data)

    def set_session_name(self, name: str) -> None:
        """Set the session name."""
        self._runtime.set_session_name(name)

    def get_session_name(self) -> str | None:
        """Get the current session name."""
        return self._runtime.get_session_name()  # type: ignore[no-any-return]

    def set_label(self, entry_id: str, label: str | None) -> None:
        """Set a label on a session entry."""
        self._runtime.set_label(entry_id, label)

    def get_active_tools(self) -> list[str]:
        """Get the list of active tool names."""
        return self._runtime.get_active_tools()  # type: ignore[no-any-return]

    def get_all_tools(self) -> list[ToolInfo]:
        """Get all registered tools."""
        return self._runtime.get_all_tools()  # type: ignore[no-any-return]

    def set_active_tools(self, tool_names: list[str]) -> None:
        """Set active tools."""
        self._runtime.set_active_tools(tool_names)

    def get_commands(self) -> list[RegisteredCommand]:
        """Get all registered commands."""
        return self._runtime.get_commands()  # type: ignore[no-any-return]

    def set_model(self, model: Any) -> Any:
        """Switch the active model."""
        return self._runtime.set_model(model)

    def get_thinking_level(self) -> Any:
        """Get the current thinking level."""
        return self._runtime.get_thinking_level()

    def set_thinking_level(self, level: Any) -> None:
        """Set the thinking level."""
        self._runtime.set_thinking_level(level)

    def register_provider(self, name: str, config: ProviderConfig) -> None:
        """Register a custom provider (deferred until runtime is initialized)."""
        self._runtime.pending_provider_registrations.append({"name": name, "config": config})

    def exec(self, command: str, args: list[str], options: Any = None) -> Any:
        """Execute a subprocess command."""
        import subprocess

        cwd = (options.get("cwd") if isinstance(options, dict) else getattr(options, "cwd", None)) or self._cwd
        try:
            result = subprocess.run(
                [command, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
            )
            return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1}


# ============================================================================
# Type guards
# ============================================================================


def is_tool_call_event_type(event_type: str) -> bool:
    """Check if an event type is a tool call event."""
    return event_type == "tool_call"


def is_tool_result_event_type(event_type: str) -> bool:
    """Check if an event type is a tool result event."""
    return event_type == "tool_result"


def is_bash_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the bash tool."""
    return (isinstance(event, (ToolResultEvent, BashToolResultEvent))) and event.tool_name == "bash"


def is_read_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the read tool."""
    return (isinstance(event, (ToolResultEvent, ReadToolResultEvent))) and event.tool_name == "read"


def is_edit_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the edit tool."""
    return (isinstance(event, (ToolResultEvent, EditToolResultEvent))) and event.tool_name == "edit"


def is_write_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the write tool."""
    return (isinstance(event, (ToolResultEvent, WriteToolResultEvent))) and event.tool_name == "write"


def is_grep_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the grep tool."""
    return (isinstance(event, (ToolResultEvent, GrepToolResultEvent))) and event.tool_name == "grep"


def is_find_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the find tool."""
    return (isinstance(event, (ToolResultEvent, FindToolResultEvent))) and event.tool_name == "find"


def is_ls_tool_result(event: Any) -> bool:
    """Check if a tool result event is from the ls tool."""
    return (isinstance(event, (ToolResultEvent, LsToolResultEvent))) and event.tool_name == "ls"


def is_session_before_event(event_type: str) -> bool:
    """Check if an event type is a session before event."""
    return event_type in (
        "session_before_switch",
        "session_before_fork",
        "session_before_compact",
        "session_before_tree",
    )
