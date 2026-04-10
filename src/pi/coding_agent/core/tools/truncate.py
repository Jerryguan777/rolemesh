"""Truncation utilities — Python port of packages/coding-agent/src/core/tools/truncate.ts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
GREP_MAX_LINE_LENGTH = 500


def format_size(bytes_count: int) -> str:
    """Format a byte count as a human-readable string."""
    if bytes_count < 1024:
        return f"{bytes_count}B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    else:
        return f"{bytes_count / (1024 * 1024):.1f}MB"


@dataclass
class TruncationResult:
    """Result of a truncation operation."""

    content: str
    truncated: bool
    truncated_by: Literal["lines", "bytes"] | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


def truncate_head(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Truncate content from the head (keep first lines/bytes).

    Returns a TruncationResult with the kept content and metadata.
    """
    encoded = content.encode("utf-8")
    total_bytes = len(encoded)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    output_lines_list: list[str] = []
    byte_count = 0
    truncated = False
    truncated_by: Literal["lines", "bytes"] | None = None

    for i, line in enumerate(lines):
        line_bytes = line.encode("utf-8")
        if i >= max_lines:
            truncated = True
            truncated_by = "lines"
            break
        if byte_count + len(line_bytes) > max_bytes:
            # Stop before this line — never return partial lines from head truncation
            truncated = True
            truncated_by = "bytes"
            break
        output_lines_list.append(line)
        byte_count += len(line_bytes)

    output_content = "".join(output_lines_list)
    output_bytes = len(output_content.encode("utf-8"))
    output_lines_count = len(output_lines_list)

    first_line_exceeds_limit = total_lines > 0 and len(lines[0].encode("utf-8")) > max_bytes

    return TruncationResult(
        content=output_content,
        truncated=truncated,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=output_lines_count,
        output_bytes=output_bytes,
        last_line_partial=False,
        first_line_exceeds_limit=first_line_exceeds_limit,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Truncate content from the tail (keep last lines/bytes).

    Returns a TruncationResult with the kept content and metadata.
    """
    encoded = content.encode("utf-8")
    total_bytes = len(encoded)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    # Check if any truncation is needed
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=total_lines > 0 and len(lines[0].encode("utf-8")) > max_bytes,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    # Collect lines from the end
    output_lines_list: list[str] = []
    byte_count = 0
    truncated_by: Literal["lines", "bytes"] | None = None
    last_line_partial = False

    # Work backwards from the end
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        line_bytes = line.encode("utf-8")
        if len(output_lines_list) >= max_lines:
            truncated_by = "lines"
            break
        if byte_count + len(line_bytes) > max_bytes:
            # Include partial line from the right
            remaining = max_bytes - byte_count
            if remaining <= 0:
                truncated_by = "bytes"
                break
            # Take last `remaining` bytes of the line
            partial_bytes = line_bytes[-remaining:]
            partial = partial_bytes.decode("utf-8", errors="replace")
            output_lines_list.insert(0, partial)
            byte_count += remaining
            truncated_by = "bytes"
            last_line_partial = True
            break
        output_lines_list.insert(0, line)
        byte_count += len(line_bytes)

    # Determine if we truncated
    truncated = len(output_lines_list) < total_lines or last_line_partial

    if truncated and truncated_by is None:
        # Means we hit the line limit exactly
        truncated_by = "lines"

    output_content = "".join(output_lines_list)
    output_bytes = len(output_content.encode("utf-8"))
    output_lines_count = len(output_lines_list)

    first_line_exceeds_limit = total_lines > 0 and len(lines[0].encode("utf-8")) > max_bytes

    return TruncationResult(
        content=output_content,
        truncated=truncated,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=output_lines_count,
        output_bytes=output_bytes,
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=first_line_exceeds_limit,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    """Truncate a single line to max_chars. Returns (text, was_truncated)."""
    if len(line) <= max_chars:
        return line, False
    return line[:max_chars], True
