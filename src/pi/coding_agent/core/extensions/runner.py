"""Extension runner - executes extensions and manages their lifecycle.

Port of packages/coding-agent/src/core/extensions/runner.ts.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import Callable
from typing import Any

from pi.agent.types import AgentMessage
from pi.coding_agent.core.extensions.types import (
    BeforeAgentStartEvent,
    ContextEvent,
    Extension,
    ExtensionActions,
    ExtensionCommandContext,
    ExtensionCommandContextActions,
    ExtensionContext,
    ExtensionContextActions,
    ExtensionError,
    ExtensionFlag,
    ExtensionRuntime,
    ExtensionShortcut,
    InputEvent,
    InputEventResult,
    RegisteredCommand,
    RegisteredTool,
    ResourcesDiscoverEvent,
    ToolCallEvent,
    ToolCallEventResult,
    ToolDefinition,
    ToolResultEvent,
    ToolResultEventResult,
    UserBashEvent,
    UserBashEventResult,
    is_session_before_event,
)

ExtensionErrorListener = Callable[[ExtensionError], None]

# Handler type aliases (from extensions/runner.ts)
NewSessionHandler = Callable[..., Any]  # (options?) -> Promise<{cancelled: bool}>
ForkHandler = Callable[[str], Any]  # (entryId) -> Promise<{cancelled: bool}>
NavigateTreeHandler = Callable[..., Any]  # (targetId, options?) -> Promise<{cancelled: bool}>
SwitchSessionHandler = Callable[[str], Any]  # (sessionPath) -> Promise<{cancelled: bool}>
ReloadHandler = Callable[[], Any]  # () -> Promise<void>
ShutdownHandler = Callable[[], None]  # () -> void


# ============================================================================
# Helper function
# ============================================================================


async def emit_session_shutdown_event(extension_runner: ExtensionRunner | None) -> bool:
    """Helper function to emit session_shutdown event to extensions.

    Returns True if the event was emitted, False if there were no handlers.
    """
    if extension_runner and extension_runner.has_handlers("session_shutdown"):
        await extension_runner.emit({"type": "session_shutdown"})
        return True
    return False


# ============================================================================
# No-op UI context
# ============================================================================

_NO_OP_UI_CONTEXT: dict[str, Any] = {
    "select": lambda *a, **k: None,
    "confirm": lambda *a, **k: False,
    "input": lambda *a, **k: None,
    "notify": lambda *a, **k: None,
    "set_status": lambda *a, **k: None,
    "set_working_message": lambda *a, **k: None,
    "set_widget": lambda *a, **k: None,
    "set_footer": lambda *a, **k: None,
    "set_header": lambda *a, **k: None,
    "set_title": lambda *a, **k: None,
    "paste_to_editor": lambda *a, **k: None,
    "set_editor_text": lambda *a, **k: None,
    "get_editor_text": lambda: "",
    "get_tools_expanded": lambda: False,
    "set_tools_expanded": lambda *a, **k: None,
}


class ExtensionRunner:
    """Manages extension lifecycle and event emission."""

    def __init__(
        self,
        extensions: list[Extension],
        runtime: ExtensionRuntime,
        cwd: str,
        session_manager: Any = None,
        model_registry: Any = None,
    ) -> None:
        self._extensions = extensions
        self._runtime = runtime
        self._ui_context: Any = None
        self._cwd = cwd
        self._session_manager = session_manager
        self._model_registry = model_registry
        self._error_listeners: set[ExtensionErrorListener] = set()
        self._get_model: Callable[[], Any] = lambda: None
        self._is_idle_fn: Callable[[], bool] = lambda: True
        self._wait_for_idle_fn: Callable[[], Any] = lambda: asyncio.sleep(0)
        self._abort_fn: Callable[[], None] = lambda: None
        self._has_pending_messages_fn: Callable[[], bool] = lambda: False
        self._get_context_usage_fn: Callable[[], Any] = lambda: None
        self._compact_fn: Callable[[Any], None] = lambda _: None
        self._get_system_prompt_fn: Callable[[], str] = lambda: ""
        self._new_session_handler: Any = lambda *a, **k: asyncio.sleep(0)
        self._fork_handler: Any = lambda *a, **k: asyncio.sleep(0)
        self._navigate_tree_handler: Any = lambda *a, **k: asyncio.sleep(0)
        self._switch_session_handler: Any = lambda *a, **k: asyncio.sleep(0)
        self._reload_handler: Any = lambda: asyncio.sleep(0)
        self._shutdown_handler: Callable[[], None] = lambda: None
        self._command_diagnostics: list[dict[str, Any]] = []

    def bind_core(
        self,
        actions: ExtensionActions,
        context_actions: ExtensionContextActions,
    ) -> None:
        """Bind core actions to the runtime and context."""
        # Copy actions into the shared runtime
        if actions.send_message:
            self._runtime.send_message = actions.send_message
        if actions.send_user_message:
            self._runtime.send_user_message = actions.send_user_message
        if actions.append_entry:
            self._runtime.append_entry = actions.append_entry
        if actions.set_session_name:
            self._runtime.set_session_name = actions.set_session_name
        if actions.get_session_name:
            self._runtime.get_session_name = actions.get_session_name
        if actions.set_label:
            self._runtime.set_label = actions.set_label
        if actions.get_active_tools:
            self._runtime.get_active_tools = actions.get_active_tools
        if actions.get_all_tools:
            self._runtime.get_all_tools = actions.get_all_tools
        if actions.set_active_tools:
            self._runtime.set_active_tools = actions.set_active_tools
        if actions.get_commands:
            self._runtime.get_commands = actions.get_commands
        if actions.set_model:
            self._runtime.set_model = actions.set_model
        if actions.get_thinking_level:
            self._runtime.get_thinking_level = actions.get_thinking_level
        if actions.set_thinking_level:
            self._runtime.set_thinking_level = actions.set_thinking_level

        # Bind context actions
        if context_actions.get_model:
            self._get_model = context_actions.get_model
        if context_actions.is_idle:
            self._is_idle_fn = context_actions.is_idle
        if context_actions.wait_for_idle:
            self._wait_for_idle_fn = context_actions.wait_for_idle
        if context_actions.abort:
            self._abort_fn = context_actions.abort
        if context_actions.has_pending_messages:
            self._has_pending_messages_fn = context_actions.has_pending_messages
        if context_actions.get_context_usage:
            self._get_context_usage_fn = context_actions.get_context_usage
        if context_actions.compact:
            self._compact_fn = context_actions.compact
        if context_actions.get_system_prompt:
            self._get_system_prompt_fn = context_actions.get_system_prompt

    def bind_command_context(
        self,
        actions: ExtensionCommandContextActions | None,
    ) -> None:
        """Bind command context actions."""
        if not actions:
            return
        if actions.wait_for_idle:
            self._wait_for_idle_fn = actions.wait_for_idle
        if actions.new_session:
            self._new_session_handler = actions.new_session
        if actions.fork:
            self._fork_handler = actions.fork
        if actions.navigate_tree:
            self._navigate_tree_handler = actions.navigate_tree
        if actions.switch_session:
            self._switch_session_handler = actions.switch_session
        if actions.reload:
            self._reload_handler = actions.reload

    def get_extension_paths(self) -> list[str]:
        """Get paths of all loaded extensions."""
        return [ext.path for ext in self._extensions]

    def get_all_registered_tools(self) -> list[RegisteredTool]:
        """Get all registered tools from all extensions."""
        tools: list[RegisteredTool] = []
        for ext in self._extensions:
            tools.extend(ext.tools.values())
        return tools

    def get_tool_definition(self, tool_name: str) -> ToolDefinition | None:
        """Get a tool definition by name."""
        for ext in self._extensions:
            tool = ext.tools.get(tool_name)
            if tool:
                return tool.definition
        return None

    def get_flags(self) -> dict[str, ExtensionFlag]:
        """Get all registered flags."""
        flags: dict[str, ExtensionFlag] = {}
        for ext in self._extensions:
            flags.update(ext.flags)
        return flags

    def set_flag_value(self, name: str, value: bool | str) -> None:
        """Set a flag value in the runtime."""
        self._runtime.flag_values[name] = value

    def get_flag_values(self) -> dict[str, bool | str]:
        """Get all flag values."""
        return dict(self._runtime.flag_values)

    def get_shortcuts(self, effective_keybindings: dict[str, Any]) -> dict[str, ExtensionShortcut]:
        """Get all registered shortcuts that don't conflict with built-in keybindings."""
        shortcuts: dict[str, ExtensionShortcut] = {}
        for ext in self._extensions:
            for shortcut_key, shortcut in ext.shortcuts.items():
                if shortcut_key.lower() not in effective_keybindings:
                    shortcuts[shortcut_key] = shortcut
        return shortcuts

    def on_error(self, listener: ExtensionErrorListener) -> Callable[[], None]:
        """Register an error listener. Returns an unsubscribe function."""
        self._error_listeners.add(listener)
        return lambda: self._error_listeners.discard(listener)

    def emit_error(self, error: ExtensionError) -> None:
        """Emit an error to all error listeners."""
        for listener in self._error_listeners:
            with contextlib.suppress(Exception):
                listener(error)

    def has_handlers(self, event_type: str) -> bool:
        """Check if any extension has handlers for the given event type."""
        for ext in self._extensions:
            handlers = ext.handlers.get(event_type)
            if handlers:
                return True
        return False

    def get_registered_commands(
        self,
        reserved: set[str] | None = None,
    ) -> list[RegisteredCommand]:
        """Get all registered commands, filtering out those that conflict with reserved names."""
        commands: list[RegisteredCommand] = []
        for ext in self._extensions:
            for command in ext.commands.values():
                if reserved and command.name in reserved:
                    msg = (
                        f"Extension command '{command.name}' from {ext.path} "
                        "conflicts with built-in commands. Skipping."
                    )
                    self._command_diagnostics.append({"type": "warning", "message": msg, "path": ext.path})
                    continue
                commands.append(command)
        return commands

    def get_command(self, name: str) -> RegisteredCommand | None:
        """Get a command by name."""
        for ext in self._extensions:
            cmd = ext.commands.get(name)
            if cmd:
                return cmd
        return None

    def shutdown(self) -> None:
        """Request a graceful shutdown."""
        self._shutdown_handler()

    def create_context(self) -> ExtensionContext:
        """Create an ExtensionContext for use in event handlers and tool execution."""
        return ExtensionContext(
            ui=self._ui_context or _NO_OP_UI_CONTEXT,
            has_ui=self._ui_context is not None,
            cwd=self._cwd,
            session_manager=self._session_manager,
            model_registry=self._model_registry,
            model=self._get_model(),
            is_idle=self._is_idle_fn,
            abort=self._abort_fn,
            has_pending_messages=self._has_pending_messages_fn,
            shutdown=self._shutdown_handler,
            get_context_usage=self._get_context_usage_fn,
            compact=self._compact_fn,
            get_system_prompt=self._get_system_prompt_fn,
        )

    def create_command_context(self) -> ExtensionCommandContext:
        """Create an ExtensionCommandContext with additional navigation capabilities."""
        ctx = self.create_context()
        return ExtensionCommandContext(
            cwd=ctx.cwd,
            has_ui=ctx.has_ui,
            ui=ctx.ui,
            session_manager=ctx.session_manager,
            model_registry=ctx.model_registry,
            model=ctx.model,
            is_idle=ctx.is_idle,
            abort=ctx.abort,
            has_pending_messages=ctx.has_pending_messages,
            shutdown=ctx.shutdown,
            get_context_usage=ctx.get_context_usage,
            compact=ctx.compact,
            get_system_prompt=ctx.get_system_prompt,
            wait_for_idle=self._wait_for_idle_fn,
            new_session=self._new_session_handler,
            fork=self._fork_handler,
            navigate_tree=self._navigate_tree_handler,
            switch_session=self._switch_session_handler,
            reload=self._reload_handler,
        )

    async def emit(self, event: Any) -> Any:
        """Emit an event to all extensions.

        For session_before_* events, returns the first cancel result.
        For all other events, returns None.
        """
        ctx = self.create_context()
        event_type = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)

        result: Any = None

        for ext in self._extensions:
            handlers = ext.handlers.get(event_type or "", [])
            for handler in handlers:
                try:
                    handler_result = await handler(event, ctx)

                    if is_session_before_event(event_type or "") and handler_result:
                        result = handler_result
                        cancel = (
                            handler_result.get("cancel")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "cancel", False)
                        )
                        if cancel:
                            return result
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event=event_type or "",
                            error=str(err),
                            stack=None,
                        )
                    )

        return result

    async def emit_tool_result(self, event: ToolResultEvent) -> ToolResultEventResult | None:
        """Emit a tool result event. Extensions can modify the result."""
        ctx = self.create_context()
        current_event = ToolResultEvent(
            tool_name=event.tool_name,
            tool_call_id=event.tool_call_id,
            input=dict(event.input),
            content=list(event.content),
            details=event.details,
            is_error=event.is_error,
        )
        modified = False

        for ext in self._extensions:
            handlers = ext.handlers.get("tool_result", [])
            for handler in handlers:
                try:
                    handler_result = await handler(current_event, ctx)
                    if not handler_result:
                        continue

                    new_content = (
                        handler_result.get("content")
                        if isinstance(handler_result, dict)
                        else getattr(handler_result, "content", None)
                    )
                    new_details = (
                        handler_result.get("details")
                        if isinstance(handler_result, dict)
                        else getattr(handler_result, "details", None)
                    )
                    new_is_error = (
                        handler_result.get("isError")
                        if isinstance(handler_result, dict)
                        else getattr(handler_result, "is_error", None)
                    )

                    if new_content is not None:
                        current_event.content = new_content
                        modified = True
                    if new_details is not None:
                        current_event.details = new_details
                        modified = True
                    if new_is_error is not None:
                        current_event.is_error = new_is_error
                        modified = True
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event="tool_result",
                            error=str(err),
                        )
                    )

        if not modified:
            return None

        return ToolResultEventResult(
            content=current_event.content,
            details=current_event.details,
            is_error=current_event.is_error,
        )

    async def emit_tool_call(self, event: ToolCallEvent) -> ToolCallEventResult | None:
        """Emit a tool call event. Extensions can block execution."""
        ctx = self.create_context()
        result: ToolCallEventResult | None = None

        for ext in self._extensions:
            handlers = ext.handlers.get("tool_call", [])
            for handler in handlers:
                handler_result = await handler(event, ctx)
                if handler_result:
                    if isinstance(handler_result, dict):
                        result = ToolCallEventResult(
                            block=handler_result.get("block", False),
                            reason=handler_result.get("reason"),
                        )
                    else:
                        result = handler_result
                    if result.block:
                        return result

        return result

    async def emit_user_bash(self, event: UserBashEvent) -> UserBashEventResult | None:
        """Emit a user bash event."""
        ctx = self.create_context()

        for ext in self._extensions:
            handlers = ext.handlers.get("user_bash", [])
            for handler in handlers:
                try:
                    handler_result = await handler(event, ctx)
                    if handler_result:
                        if isinstance(handler_result, dict):
                            return UserBashEventResult(allow=handler_result.get("allow", False))
                        return handler_result  # type: ignore[no-any-return]
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event="user_bash",
                            error=str(err),
                        )
                    )

        return None

    async def emit_context(self, messages: list[AgentMessage]) -> list[AgentMessage]:
        """Emit a context event. Extensions can modify the message list."""
        ctx = self.create_context()
        current_messages: list[AgentMessage] = copy.deepcopy(messages)

        for ext in self._extensions:
            handlers = ext.handlers.get("context", [])
            for handler in handlers:
                try:
                    event = ContextEvent(type="context", messages=list(current_messages))
                    handler_result = await handler(event, ctx)
                    if handler_result:
                        new_msgs = (
                            handler_result.get("messages")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "messages", None)
                        )
                        if new_msgs is not None:
                            current_messages = new_msgs
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event="context",
                            error=str(err),
                        )
                    )

        return current_messages

    async def emit_before_agent_start(
        self,
        prompt: str,
        images: list[Any] | None,
        system_prompt: str,
    ) -> dict[str, Any] | None:
        """Emit before_agent_start event. Returns combined result or None."""
        ctx = self.create_context()
        messages: list[Any] = []
        current_system_prompt = system_prompt
        system_prompt_modified = False

        for ext in self._extensions:
            handlers = ext.handlers.get("before_agent_start", [])
            for handler in handlers:
                try:
                    event = BeforeAgentStartEvent(
                        type="before_agent_start",
                        prompt=prompt,
                        images=images,
                        system_prompt=current_system_prompt,
                    )
                    handler_result = await handler(event, ctx)
                    if handler_result:
                        msg = (
                            handler_result.get("message")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "message", None)
                        )
                        new_sp = (
                            handler_result.get("systemPrompt")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "system_prompt", None)
                        )
                        if msg is not None:
                            messages.append(msg)
                        if new_sp is not None:
                            current_system_prompt = new_sp
                            system_prompt_modified = True
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event="before_agent_start",
                            error=str(err),
                        )
                    )

        if messages or system_prompt_modified:
            return {
                "messages": messages if messages else None,
                "system_prompt": current_system_prompt if system_prompt_modified else None,
            }

        return None

    async def emit_resources_discover(self, cwd: str, reason: str) -> dict[str, Any]:
        """Emit resources_discover event. Collects paths from all extensions."""
        ctx = self.create_context()
        skill_paths: list[dict[str, str]] = []
        prompt_paths: list[dict[str, str]] = []
        theme_paths: list[dict[str, str]] = []

        for ext in self._extensions:
            handlers = ext.handlers.get("resources_discover", [])
            for handler in handlers:
                try:
                    event = ResourcesDiscoverEvent(
                        type="resources_discover",
                        cwd=cwd,
                        reason=reason,
                    )
                    handler_result = await handler(event, ctx)
                    if handler_result:
                        sp = (
                            handler_result.get("skillPaths")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "skill_paths", None)
                        )
                        pp = (
                            handler_result.get("promptPaths")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "prompt_paths", None)
                        )
                        tp = (
                            handler_result.get("themePaths")
                            if isinstance(handler_result, dict)
                            else getattr(handler_result, "theme_paths", None)
                        )
                        if sp:
                            skill_paths.extend({"path": p, "extension_path": ext.path} for p in sp)
                        if pp:
                            prompt_paths.extend({"path": p, "extension_path": ext.path} for p in pp)
                        if tp:
                            theme_paths.extend({"path": p, "extension_path": ext.path} for p in tp)
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event="resources_discover",
                            error=str(err),
                        )
                    )

        return {
            "skill_paths": skill_paths,
            "prompt_paths": prompt_paths,
            "theme_paths": theme_paths,
        }

    async def emit_input(
        self,
        text: str,
        images: list[Any] | None,
        source: str,
    ) -> InputEventResult:
        """Emit input event. Transforms chain, "handled" short-circuits."""
        ctx = self.create_context()
        current_text = text
        current_images = images

        for ext in self._extensions:
            handlers = ext.handlers.get("input", [])
            for handler in handlers:
                try:
                    event = InputEvent(
                        type="input",
                        text=current_text,
                        images=current_images,
                        source=source,
                    )
                    result = await handler(event, ctx)
                    if result:
                        action = (
                            result.get("action") if isinstance(result, dict) else getattr(result, "action", "continue")
                        )
                        if action == "handled":
                            if isinstance(result, dict):
                                return InputEventResult(
                                    action="handled",
                                    text=result.get("text"),
                                    images=result.get("images"),
                                )
                            return result  # type: ignore[no-any-return]
                        if action == "transform":
                            new_text = result.get("text") if isinstance(result, dict) else getattr(result, "text", None)
                            new_images = (
                                result.get("images") if isinstance(result, dict) else getattr(result, "images", None)
                            )
                            if new_text is not None:
                                current_text = new_text
                            if new_images is not None:
                                current_images = new_images
                except Exception as err:
                    self.emit_error(
                        ExtensionError(
                            extension_path=ext.path,
                            event="input",
                            error=str(err),
                        )
                    )

        if current_text != text or current_images is not images:
            return InputEventResult(
                action="transform",
                text=current_text,
                images=current_images,
            )
        return InputEventResult(action="continue")
