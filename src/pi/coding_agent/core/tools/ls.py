"""Ls tool — Python port of packages/coding-agent/src/core/tools/ls.ts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .path_utils import resolve_to_cwd
from .truncate import TruncationResult


@dataclass
class LsToolInput:
    """Input parameters for the ls tool."""

    path: str | None = None
    limit: int | None = None


@dataclass
class LsToolDetails:
    """Details returned by the ls tool."""

    truncation: TruncationResult | None = None
    entry_limit_reached: int | None = None


@dataclass
class LsOperations:
    """Pluggable operations for the ls tool.

    Override to delegate directory listing to remote systems (e.g., SSH).
    """

    exists: Callable[[str], Awaitable[bool] | bool]
    """Check if path exists."""
    stat: Callable[[str], Awaitable[Any] | Any]
    """Get file/directory stats. Raises if not found."""
    readdir: Callable[[str], Awaitable[list[str]] | list[str]]
    """Read directory entries."""


@dataclass
class LsToolOptions:
    """Options for the ls tool."""

    operations: LsOperations | None = None
    """Custom operations for directory listing. Default: local filesystem."""


DEFAULT_LIMIT = 500


class LsTool(AgentTool):
    """List directory contents."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "ls"

    @property
    def label(self) -> str:
        return "ls"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Directories are shown with a trailing '/'. "
            f"Results are limited to {DEFAULT_LIMIT} entries by default."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory to list (optional, defaults to cwd)",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of entries to return (default {DEFAULT_LIMIT})",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """List directory contents."""
        path_str: str | None = params.get("path")
        limit: int = int(params.get("limit") or DEFAULT_LIMIT)

        resolved = resolve_to_cwd(path_str, self._cwd) if path_str else self._cwd
        directory = Path(resolved)

        if not directory.exists():
            raise RuntimeError(f"Path not found: {resolved}")
        if not directory.is_dir():
            raise RuntimeError(f"Not a directory: {resolved}")

        # List entries, sorted case-insensitively
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())

        lines: list[str] = []
        for entry in entries:
            if entry.is_dir():
                lines.append(entry.name + "/")
            else:
                lines.append(entry.name)

            if len(lines) >= limit:
                break

        total = len(entries)
        truncated = total > limit
        output = "\n".join(lines)

        if truncated:
            output += f"\n\n[Showing {limit} of {total} entries]"

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details={"entry_count": len(lines), "truncated": truncated},
        )
