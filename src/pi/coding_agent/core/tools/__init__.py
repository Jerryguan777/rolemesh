"""Tools package for pi.coding_agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pi.agent.types import AgentTool

from .bash import (
    BashSpawnContext,
    BashSpawnHook,
    BashTool,
    BashToolDetails,
    BashToolInput,
    BashToolOptions,
)
from .edit import (
    EditOperations,
    EditTool,
    EditToolDetails,
    EditToolInput,
    EditToolOptions,
)
from .edit_diff import (
    DiffResult,
    EditDiffError,
    EditDiffResult,
    FuzzyMatchResult,
    compute_edit_diff,
    detect_line_ending,
    fuzzy_find_text,
    generate_diff_string,
    normalize_for_fuzzy_match,
    normalize_to_lf,
    restore_line_endings,
    strip_bom,
)
from .find import (
    FindOperations,
    FindTool,
    FindToolDetails,
    FindToolInput,
    FindToolOptions,
)
from .grep import (
    GrepOperations,
    GrepTool,
    GrepToolDetails,
    GrepToolInput,
    GrepToolOptions,
)
from .ls import (
    LsOperations,
    LsTool,
    LsToolDetails,
    LsToolInput,
    LsToolOptions,
)
from .read import (
    ReadOperations,
    ReadTool,
    ReadToolDetails,
    ReadToolInput,
    ReadToolOptions,
)
from .truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    GREP_MAX_LINE_LENGTH,
    TruncationResult,
    format_size,
    truncate_head,
    truncate_line,
    truncate_tail,
)
from .write import (
    WriteOperations,
    WriteTool,
    WriteToolInput,
    WriteToolOptions,
)

# Tool type alias (AgentTool from pi-agent)
Tool = AgentTool

ToolName = Literal["read", "bash", "edit", "write", "grep", "find", "ls"]


@dataclass
class TruncationOptions:
    """Options for truncation operations."""

    max_lines: int | None = None  # Default: 2000
    max_bytes: int | None = None  # Default: 50KB


@dataclass
class ToolsOptions:
    """Options for tool creation."""

    read: ReadToolOptions | None = None
    bash: BashToolOptions | None = None


@dataclass
class ToolExecutionOptions:
    """Options for tool execution rendering."""

    show_images: bool = True  # Only used if terminal supports images


@dataclass
class ToolHtmlRendererDeps:
    """Dependencies for the tool HTML renderer."""

    get_tool_definition: Any = None  # Callable[[str], ToolDefinition | None]
    theme: Any = None  # Theme
    width: int = 100


# Aliases for default_max constants (snake_case naming)
default_max_bytes = DEFAULT_MAX_BYTES
default_max_lines = DEFAULT_MAX_LINES
grep_max_line_length = GREP_MAX_LINE_LENGTH


def create_bash_tool(cwd: str, options: BashToolOptions | None = None) -> AgentTool:
    """Create a bash tool configured for a specific working directory."""
    # TODO: BashToolOptions support (command_prefix, spawn_hook)
    return BashTool(cwd)


def create_edit_tool(cwd: str, options: EditToolOptions | None = None) -> AgentTool:
    """Create an edit tool configured for a specific working directory."""
    # TODO: EditToolOptions support (custom operations)
    return EditTool(cwd)


def create_read_tool(cwd: str, options: ReadToolOptions | None = None) -> AgentTool:
    """Create a read tool configured for a specific working directory."""
    # TODO: ReadToolOptions support (auto_resize_images, custom operations)
    return ReadTool(cwd)


def create_write_tool(cwd: str, options: WriteToolOptions | None = None) -> AgentTool:
    """Create a write tool configured for a specific working directory."""
    # TODO: WriteToolOptions support (custom operations)
    return WriteTool(cwd)


def create_find_tool(cwd: str, options: FindToolOptions | None = None) -> AgentTool:
    """Create a find tool configured for a specific working directory."""
    # TODO: FindToolOptions support (custom operations)
    return FindTool(cwd)


def create_grep_tool(cwd: str, options: GrepToolOptions | None = None) -> AgentTool:
    """Create a grep tool configured for a specific working directory."""
    # TODO: GrepToolOptions support (custom operations)
    return GrepTool(cwd)


def create_ls_tool(cwd: str, options: LsToolOptions | None = None) -> AgentTool:
    """Create an ls tool configured for a specific working directory."""
    # TODO: LsToolOptions support (custom operations)
    return LsTool(cwd)


def create_tool_html_renderer(deps: dict[str, Any]) -> dict[str, Any]:
    """Create a tool HTML renderer.

    The renderer looks up tool definitions and invokes their renderCall/renderResult
    methods, converting the resulting TUI Component output (ANSI) to HTML.

    Args:
        deps: Dict with keys: get_tool_definition (callable), theme, width (default 100).

    Returns:
        Dict with render_call(tool_name, args) and render_result(tool_name, result, details, is_error).
    """
    from pi.coding_agent.core.export_html.ansi_to_html import ansi_lines_to_html

    get_tool_definition = deps.get("get_tool_definition")
    theme = deps.get("theme")
    width: int = deps.get("width", 100)

    def render_call(tool_name: str, args: Any) -> str | None:
        try:
            if get_tool_definition is None:
                return None
            tool_def = get_tool_definition(tool_name)
            if tool_def is None:
                return None
            render_call_fn = getattr(tool_def, "render_call", None)
            if render_call_fn is None:
                return None
            component = render_call_fn(args, theme)
            lines: list[str] = component.render(width)
            return ansi_lines_to_html(lines)
        except Exception:
            return None

    def render_result(
        tool_name: str,
        result: list[dict[str, Any]],
        details: Any,
        is_error: bool,
    ) -> str | None:
        try:
            if get_tool_definition is None:
                return None
            tool_def = get_tool_definition(tool_name)
            if tool_def is None:
                return None
            render_result_fn = getattr(tool_def, "render_result", None)
            if render_result_fn is None:
                return None
            agent_tool_result = {
                "content": result,
                "details": details,
                "isError": is_error,
            }
            component = render_result_fn(
                agent_tool_result,
                {"expanded": True, "isPartial": False},
                theme,
            )
            lines: list[str] = component.render(width)
            return ansi_lines_to_html(lines)
        except Exception:
            return None

    return {"render_call": render_call, "render_result": render_result}


def create_coding_tools(cwd: str, **kwargs: Any) -> list[AgentTool]:
    """Create coding tools: read, bash, edit, write."""
    return [ReadTool(cwd), BashTool(cwd), EditTool(cwd), WriteTool(cwd)]


def create_read_only_tools(cwd: str, **kwargs: Any) -> list[AgentTool]:
    """Create read-only tools: read, grep, find, ls."""
    return [ReadTool(cwd), GrepTool(cwd), FindTool(cwd), LsTool(cwd)]


def create_all_tools(cwd: str, **kwargs: Any) -> dict[str, AgentTool]:
    """Create all tools configured for a working directory."""
    return {
        "read": ReadTool(cwd),
        "bash": BashTool(cwd),
        "edit": EditTool(cwd),
        "write": WriteTool(cwd),
        "grep": GrepTool(cwd),
        "find": FindTool(cwd),
        "ls": LsTool(cwd),
    }


# Convenience module-level tool instances (use cwd=".")
bash_tool = BashTool(".")
edit_tool = EditTool(".")
read_tool = ReadTool(".")
write_tool = WriteTool(".")
find_tool = FindTool(".")
grep_tool = GrepTool(".")
ls_tool = LsTool(".")

coding_tools: list[AgentTool] = [read_tool, bash_tool, edit_tool, write_tool]
read_only_tools: list[AgentTool] = [read_tool, grep_tool, find_tool, ls_tool]
all_tools: dict[str, AgentTool] = {
    "read": read_tool,
    "bash": bash_tool,
    "edit": edit_tool,
    "write": write_tool,
    "grep": grep_tool,
    "find": find_tool,
    "ls": ls_tool,
}


__all__ = [
    # Constants
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_LINES",
    "GREP_MAX_LINE_LENGTH",
    # Bash-specific types
    "BashSpawnContext",
    "BashSpawnHook",
    # Tool classes
    "BashTool",
    # Tool details types
    "BashToolDetails",
    # Tool input types
    "BashToolInput",
    # Tool options types
    "BashToolOptions",
    # Edit diff types
    "DiffResult",
    "EditDiffError",
    "EditDiffResult",
    # Operations types
    "EditOperations",
    "EditTool",
    "EditToolDetails",
    "EditToolInput",
    "EditToolOptions",
    "FindOperations",
    "FindTool",
    "FindToolDetails",
    "FindToolInput",
    "FindToolOptions",
    "FuzzyMatchResult",
    "GrepOperations",
    "GrepTool",
    "GrepToolDetails",
    "GrepToolInput",
    "GrepToolOptions",
    "LsOperations",
    "LsTool",
    "LsToolDetails",
    "LsToolInput",
    "LsToolOptions",
    "ReadOperations",
    "ReadTool",
    "ReadToolDetails",
    "ReadToolInput",
    "ReadToolOptions",
    # Type aliases
    "Tool",
    "ToolExecutionOptions",
    "ToolHtmlRendererDeps",
    "ToolName",
    "ToolsOptions",
    # Truncation
    "TruncationOptions",
    "TruncationResult",
    "WriteOperations",
    "WriteTool",
    "WriteToolInput",
    "WriteToolOptions",
    "all_tools",
    # Default tool instances
    "bash_tool",
    "coding_tools",
    "compute_edit_diff",
    "create_all_tools",
    # Factory functions
    "create_bash_tool",
    "create_coding_tools",
    "create_edit_tool",
    "create_find_tool",
    "create_grep_tool",
    "create_ls_tool",
    "create_read_only_tools",
    "create_read_tool",
    "create_tool_html_renderer",
    "create_write_tool",
    "default_max_bytes",
    "default_max_lines",
    # Edit diff utilities
    "detect_line_ending",
    "edit_tool",
    "find_tool",
    "format_size",
    "fuzzy_find_text",
    "generate_diff_string",
    "grep_max_line_length",
    "grep_tool",
    "ls_tool",
    "normalize_for_fuzzy_match",
    "normalize_to_lf",
    "read_only_tools",
    "read_tool",
    "restore_line_endings",
    "strip_bom",
    "truncate_head",
    "truncate_line",
    "truncate_tail",
    "write_tool",
]
