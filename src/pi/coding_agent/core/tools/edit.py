"""Edit tool — Python port of packages/coding-agent/src/core/tools/edit.ts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .edit_diff import (
    detect_line_ending,
    fuzzy_find_text,
    generate_diff_string,
    normalize_for_fuzzy_match,
    normalize_to_lf,
    restore_line_endings,
    strip_bom,
)
from .path_utils import resolve_to_cwd


@dataclass
class EditToolInput:
    """Input parameters for the edit tool."""

    path: str
    old_text: str
    new_text: str


@dataclass
class EditToolDetails:
    """Details returned by the edit tool."""

    diff: str
    """Unified diff of the changes made."""
    first_changed_line: int | None = None
    """Line number of the first change in the new file."""


@dataclass
class EditOperations:
    """Pluggable operations for the edit tool.

    Override to delegate file editing to remote systems (e.g., SSH).
    """

    read_file: Callable[[str], Awaitable[bytes]]
    """Read file contents as bytes."""
    write_file: Callable[[str, str], Awaitable[None]]
    """Write content to a file."""
    access: Callable[[str], Awaitable[None]]
    """Check if file is readable and writable (raise if not)."""


@dataclass
class EditToolOptions:
    """Options for the edit tool."""

    operations: EditOperations | None = None
    """Custom operations for file editing. Default: local filesystem."""


class EditTool(AgentTool):
    """Edit a file by replacing a specific text with new text."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "edit"

    @property
    def label(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "The old_text must exist exactly once in the file. "
            "Uses fuzzy matching to handle minor whitespace/quote differences."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "old_text": {
                    "type": "string",
                    "description": "Text to replace (must be unique in the file)",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Edit a file by replacing old_text with new_text."""
        file_path_str: str = params["path"]
        old_text: str = params["old_text"]
        new_text: str = params["new_text"]

        resolved = resolve_to_cwd(file_path_str, self._cwd)
        path = Path(resolved)

        if not path.exists():
            raise RuntimeError(f"File not found: {resolved}")
        if not path.is_file():
            raise RuntimeError(f"Not a file: {resolved}")

        raw_content = path.read_text(encoding="utf-8", errors="replace")

        # Strip and remember BOM
        bom, content = strip_bom(raw_content)

        # Detect and normalize line endings
        line_ending = detect_line_ending(content)
        content_lf = normalize_to_lf(content)
        old_text_lf = normalize_to_lf(old_text)
        new_text_lf = normalize_to_lf(new_text)

        # Check for multiple occurrences before replacing
        first_match = fuzzy_find_text(content_lf, old_text_lf)
        if not first_match.found:
            raise RuntimeError(
                f"old_text not found in file: {resolved}\nMake sure old_text exactly matches the content in the file."
            )

        # Check for multiple occurrences (using exact match for efficiency)
        first_occurrence = content_lf.find(old_text_lf)
        if first_occurrence != -1:
            second_occurrence = content_lf.find(old_text_lf, first_occurrence + len(old_text_lf))
            if second_occurrence != -1:
                raise RuntimeError(
                    f"old_text appears multiple times in file: {resolved}\n"
                    f"Provide more context to make old_text unique."
                )
        else:
            # Fuzzy match was used; check for multiple fuzzy occurrences
            normalized_content = normalize_for_fuzzy_match(content_lf)
            normalized_old = normalize_for_fuzzy_match(old_text_lf)
            fi = normalized_content.find(normalized_old)
            if fi != -1:
                si = normalized_content.find(normalized_old, fi + len(normalized_old))
                if si != -1:
                    raise RuntimeError(
                        f"old_text appears multiple times in file (fuzzy): {resolved}\n"
                        f"Provide more context to make old_text unique."
                    )

        # Perform replacement using the match we already found
        match = first_match
        new_content_lf = content_lf[: match.index] + new_text_lf + content_lf[match.index + match.match_length :]

        # Restore line endings and BOM
        new_content = restore_line_endings(new_content_lf, line_ending)
        final_content = bom + new_content

        # Generate diff for display
        diff_result = generate_diff_string(content_lf, new_content_lf)

        # Write the file
        path.write_text(final_content, encoding="utf-8")

        fuzzy_note = " (used fuzzy matching)" if match.used_fuzzy_match else ""
        result_text = f"Edited {resolved}{fuzzy_note}\n\n{diff_result.diff}"

        return AgentToolResult(
            content=[TextContent(type="text", text=result_text)],
            details={
                "diff": diff_result.diff,
                "first_changed_line": diff_result.first_changed_line,
                "used_fuzzy_match": match.used_fuzzy_match,
            },
        )
