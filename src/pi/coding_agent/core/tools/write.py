"""Write tool — Python port of packages/coding-agent/src/core/tools/write.ts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .path_utils import resolve_to_cwd


@dataclass
class WriteToolInput:
    """Input parameters for the write tool."""

    path: str
    content: str


@dataclass
class WriteOperations:
    """Pluggable operations for the write tool.

    Override to delegate file writing to remote systems (e.g., SSH).
    """

    write_file: Callable[[str, str], Awaitable[None]]
    """Write content to a file."""
    mkdir: Callable[[str], Awaitable[None]]
    """Create directory (recursively)."""


@dataclass
class WriteToolOptions:
    """Options for the write tool."""

    operations: WriteOperations | None = None
    """Custom operations for file writing. Default: local filesystem."""


class WriteTool(AgentTool):
    """Write content to a file, creating parent directories as needed."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "write"

    @property
    def label(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file. Creates the file and any missing parent directories. Overwrites existing files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write"},
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Write content to a file."""
        file_path_str: str = params["path"]
        content: str = params["content"]

        resolved = resolve_to_cwd(file_path_str, self._cwd)
        path = Path(resolved)

        # Create parent directories if needed
        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(content, encoding="utf-8")

        return AgentToolResult(
            content=[TextContent(type="text", text=f"Written to {resolved}")],
            details=None,
        )
