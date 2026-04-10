"""Grep tool — Python port of packages/coding-agent/src/core/tools/grep.ts."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .path_utils import resolve_to_cwd
from .truncate import TruncationResult, truncate_line


@dataclass
class GrepToolInput:
    """Input parameters for the grep tool."""

    pattern: str
    path: str | None = None
    glob: str | None = None
    ignore_case: bool = False
    literal: bool = False
    context: int | None = None
    limit: int | None = None


@dataclass
class GrepToolDetails:
    """Details returned by the grep tool."""

    truncation: TruncationResult | None = None
    match_limit_reached: int | None = None
    lines_truncated: bool | None = None


@dataclass
class GrepOperations:
    """Pluggable operations for the grep tool.

    Override to delegate search to remote systems (e.g., SSH).
    """

    is_directory: Callable[[str], Awaitable[bool] | bool]
    """Check if path is a directory. Raises if path doesn't exist."""
    read_file: Callable[[str], Awaitable[str] | str]
    """Read file contents for context lines."""


@dataclass
class GrepToolOptions:
    """Options for the grep tool."""

    operations: GrepOperations | None = None
    """Custom operations for grep. Default: local filesystem + ripgrep."""


DEFAULT_LIMIT = 100


class GrepTool(AgentTool):
    """Search for patterns in files using ripgrep."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "grep"

    @property
    def label(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search for a pattern in files using ripgrep. "
            "Supports regex patterns, case-insensitive search, literal strings, and context lines."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex or literal)"},
                "path": {
                    "type": "string",
                    "description": "File or directory to search (optional, defaults to cwd)",
                },
                "glob": {
                    "type": "string",
                    "description": "Glob pattern to filter files (e.g. '*.py')",
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default false)",
                },
                "literal": {
                    "type": "boolean",
                    "description": "Treat pattern as a literal string, not regex (default false)",
                },
                "context": {
                    "type": "integer",
                    "description": "Number of context lines around each match",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of results to return (default {DEFAULT_LIMIT})",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Search for a pattern in files."""
        pattern: str = params["pattern"]
        path_str: str | None = params.get("path")
        glob_pattern: str | None = params.get("glob")
        ignore_case: bool = bool(params.get("ignore_case", False))
        literal: bool = bool(params.get("literal", False))
        context: int | None = params.get("context")
        limit: int = int(params.get("limit") or DEFAULT_LIMIT)

        search_path = resolve_to_cwd(path_str, self._cwd) if path_str else self._cwd

        rg_path = shutil.which("rg")
        if rg_path is None:
            raise RuntimeError(
                "ripgrep (rg) is not available. Please install it: https://github.com/BurntSushi/ripgrep"
            )

        results = await _grep_with_rg(
            rg_path,
            pattern,
            search_path,
            glob_pattern,
            ignore_case,
            literal,
            context,
            limit,
        )

        if not results:
            return AgentToolResult(
                content=[TextContent(type="text", text="No matches found.")],
                details={"match_count": 0},
            )

        truncated = len(results) >= limit
        output = "\n".join(results)
        if truncated:
            output += f"\n\n[Results limited to {limit} matches]"

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details={"match_count": len(results), "truncated": truncated},
        )


async def _grep_with_rg(
    rg_path: str,
    pattern: str,
    search_path: str,
    glob_pattern: str | None,
    ignore_case: bool,
    literal: bool,
    context: int | None,
    limit: int,
) -> list[str]:
    """Run ripgrep and parse its JSON output."""
    cmd: list[str] = [
        rg_path,
        "--json",
        "--line-number",
        "--color=never",
        "--hidden",
    ]

    if ignore_case:
        cmd.append("--ignore-case")
    if literal:
        cmd.append("--fixed-strings")
    if context is not None:
        cmd.extend(["--context", str(context)])
    if glob_pattern:
        cmd.extend(["--glob", glob_pattern])

    cmd.extend(["--", pattern, search_path])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except TimeoutError:
        raise RuntimeError("grep timed out after 30 seconds") from None
    except Exception as e:
        raise RuntimeError(f"grep failed: {e}") from e

    results: list[str] = []
    for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "match":
            data = event.get("data", {})
            path_data = data.get("path", {})
            file_path = path_data.get("text", "")
            line_num = data.get("line_number", 0)
            lines_data = data.get("lines", {})
            text = lines_data.get("text", "").rstrip("\n")
            truncated_text, _ = truncate_line(text)
            results.append(f"{file_path}:{line_num}: {truncated_text}")
        elif event_type == "context":
            data = event.get("data", {})
            path_data = data.get("path", {})
            file_path = path_data.get("text", "")
            line_num = data.get("line_number", 0)
            lines_data = data.get("lines", {})
            text = lines_data.get("text", "").rstrip("\n")
            truncated_text, _ = truncate_line(text)
            results.append(f"{file_path}:{line_num}| {truncated_text}")

        if len(results) >= limit:
            break

    return results


def _get_search_path(path: Path) -> list[Path]:
    """Return list of files to search under path (unused, kept for reference)."""
    return [f for f in path.rglob("*") if f.is_file()]
