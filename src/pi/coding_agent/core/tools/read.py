"""Read tool — Python port of packages/coding-agent/src/core/tools/read.ts."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import ImageContent, TextContent

from .path_utils import resolve_read_path
from .truncate import DEFAULT_MAX_BYTES, TruncationResult, format_size, truncate_head


@dataclass
class ReadToolInput:
    """Input parameters for the read tool."""

    path: str
    offset: int | None = None
    limit: int | None = None


@dataclass
class ReadToolDetails:
    """Details returned by the read tool."""

    truncation: TruncationResult | None = None


@dataclass
class ReadOperations:
    """Pluggable operations for the read tool.

    Override to delegate file reading to remote systems (e.g., SSH).
    """

    read_file: Callable[[str], Awaitable[bytes]]
    """Read file contents as bytes."""
    access: Callable[[str], Awaitable[None]]
    """Check if file is readable (raise if not)."""
    detect_image_mime_type: Callable[[str], Awaitable[str | None]] | None = None
    """Detect image MIME type, return None for non-images."""


@dataclass
class ReadToolOptions:
    """Options for the read tool."""

    auto_resize_images: bool = True
    """Whether to auto-resize images to 2000x2000 max."""
    operations: ReadOperations | None = None
    """Custom operations for file reading. Default: local filesystem."""


# Supported image extensions and their MIME types
SUPPORTED_IMAGE_EXTENSIONS: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def detect_image_mime_type(path: Path) -> str | None:
    """Return MIME type if file is a supported image, else None."""
    return SUPPORTED_IMAGE_EXTENSIONS.get(path.suffix.lower())


class ReadTool(AgentTool):
    """Read file contents, with optional offset/limit."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "read"

    @property
    def label(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Supports text files and images (jpg, jpeg, png, gif, webp). "
            "Use offset (1-indexed line number) and limit to read specific portions of large files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
                "offset": {
                    "type": "integer",
                    "description": "1-indexed line number to start reading from (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (optional)",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Read a file and return its contents."""
        file_path_str: str = params["path"]
        offset: int | None = params.get("offset")
        limit: int | None = params.get("limit")

        resolved = resolve_read_path(file_path_str, self._cwd)
        path = Path(resolved)

        if not path.exists():
            raise RuntimeError(f"File not found: {resolved}")
        if not path.is_file():
            raise RuntimeError(f"Not a file: {resolved}")

        # Check if image
        mime_type = detect_image_mime_type(path)
        if mime_type is not None:
            raw = path.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            return AgentToolResult(
                content=[
                    ImageContent(
                        type="image",
                        mime_type=mime_type,
                        data=encoded,
                    )
                ],
                details=None,
            )

        # Text file
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        # Apply offset (1-indexed)
        start_idx = 0
        if offset is not None:
            start_idx = max(0, offset - 1)

        first_line_num = start_idx + 1

        # Apply limit
        selected_lines = lines[start_idx : start_idx + limit] if limit is not None else lines[start_idx:]

        selected_content = "".join(selected_lines)

        # Truncate if necessary
        trunc = truncate_head(selected_content)

        # If the first line alone exceeds the byte limit, return a helpful message
        if trunc.first_line_exceeds_limit:
            first_line_size = format_size(len(lines[start_idx].encode("utf-8")))
            output_text = (
                f"[Line {first_line_num} is {first_line_size}, "
                f"exceeds {format_size(DEFAULT_MAX_BYTES)} limit. "
                f"Use bash: sed -n '{first_line_num}p' {file_path_str} | head -c {DEFAULT_MAX_BYTES}]"
            )
            return AgentToolResult(
                content=[TextContent(type="text", text=output_text)],
                details={"truncation": trunc},
            )

        output_text = trunc.content

        # Add line number prefix
        numbered_lines = []
        for i, line in enumerate(output_text.splitlines(keepends=True)):
            line_num = first_line_num + i
            # Strip existing newline for display
            stripped = line.rstrip("\n").rstrip("\r")
            numbered_lines.append(f"{line_num}\t{stripped}\n")
        output_text = "".join(numbered_lines)

        # Add notices
        notices: list[str] = []
        if offset is not None and start_idx > 0:
            notices.append(f"[Showing from line {first_line_num}]")
        if trunc.truncated:
            shown_end = first_line_num + trunc.output_lines - 1
            notices.append(f"[Truncated: showing lines {first_line_num}-{shown_end} of {total_lines}]")

        if notices:
            output_text = "\n".join(notices) + "\n" + output_text

        return AgentToolResult(
            content=[TextContent(type="text", text=output_text)],
            details={"truncation": trunc} if trunc.truncated else None,
        )
