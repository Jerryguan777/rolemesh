"""Find tool — Python port of packages/coding-agent/src/core/tools/find.ts."""

from __future__ import annotations

import asyncio
import glob as glob_module
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .path_utils import resolve_to_cwd
from .truncate import TruncationResult


@dataclass
class FindToolInput:
    """Input parameters for the find tool."""

    pattern: str
    path: str | None = None
    limit: int | None = None


@dataclass
class FindToolDetails:
    """Details returned by the find tool."""

    truncation: TruncationResult | None = None
    result_limit_reached: int | None = None


@dataclass
class FindOperations:
    """Pluggable operations for the find tool.

    Override to delegate file search to remote systems (e.g., SSH).
    """

    exists: Callable[[str], Awaitable[bool] | bool]
    """Check if path exists."""
    glob: Callable[[str, str, dict[str, Any]], Awaitable[list[str]] | list[str]]
    """Find files matching glob pattern. Returns relative paths."""


@dataclass
class FindToolOptions:
    """Options for the find tool."""

    operations: FindOperations | None = None
    """Custom operations for find. Default: local filesystem + fd."""


DEFAULT_LIMIT = 1000

# Patterns to exclude
_EXCLUDED_PARTS = frozenset(["node_modules", ".git"])


class FindTool(AgentTool):
    """Find files matching a glob pattern."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "find"

    @property
    def label(self) -> str:
        return "find"

    @property
    def description(self) -> str:
        return (
            "Find files matching a glob pattern. "
            "Excludes node_modules and .git directories. "
            f"Results are limited to {DEFAULT_LIMIT} by default."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match (e.g. '**/*.py', '*.ts')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (optional, defaults to cwd)",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of results (default {DEFAULT_LIMIT})",
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
        """Find files matching a glob pattern."""
        pattern: str = params["pattern"]
        path_str: str | None = params.get("path")
        limit: int = int(params.get("limit") or DEFAULT_LIMIT)

        search_path = resolve_to_cwd(path_str, self._cwd) if path_str else self._cwd

        fd_path = shutil.which("fd")
        if fd_path:
            results = await _find_with_fd(fd_path, pattern, search_path, limit)
        else:
            results = _find_with_glob(pattern, search_path, limit)

        if not results:
            return AgentToolResult(
                content=[TextContent(type="text", text="No files found.")],
                details={"count": 0},
            )

        truncated = len(results) >= limit
        output = "\n".join(results)
        if truncated:
            output += f"\n\n[Results limited to {limit} files]"

        return AgentToolResult(
            content=[TextContent(type="text", text=output)],
            details={"count": len(results), "truncated": truncated},
        )


async def _find_with_fd(
    fd_path: str,
    pattern: str,
    search_path: str,
    limit: int,
) -> list[str]:
    """Use the fd binary to find files (async)."""
    cmd: list[str] = [
        fd_path,
        "--glob",
        pattern,
        search_path,
        "--max-results",
        str(limit),
        "--hidden",
        "--exclude",
        "node_modules",
        "--exclude",
        ".git",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except TimeoutError:
        raise RuntimeError("find timed out after 30 seconds") from None
    except Exception:
        # Fall back to Python glob
        return _find_with_glob(pattern, search_path, limit)

    lines = [line for line in stdout_bytes.decode("utf-8", errors="replace").splitlines() if line.strip()]
    # Relativize absolute paths returned by fd
    root = Path(search_path)
    relative: list[str] = []
    for line in lines:
        p = Path(line)
        try:
            relative.append(str(p.relative_to(root)))
        except ValueError:
            relative.append(line)
    return sorted(relative)[:limit]


def _is_excluded(path: Path, root: Path) -> bool:
    """Return True if any path component is in the exclusion list."""
    try:
        rel = path.relative_to(root)
        parts = rel.parts
    except ValueError:
        parts = path.parts

    return any(part in _EXCLUDED_PARTS for part in parts)


def _find_with_glob(
    pattern: str,
    search_path: str,
    limit: int,
) -> list[str]:
    """Use Python's glob module to find files."""
    raw_results = glob_module.glob(pattern, root_dir=search_path, recursive=True)

    filtered: list[str] = []
    for rel_str in raw_results:
        rel_path = Path(rel_str)

        # Filter excluded directories
        if any(part in _EXCLUDED_PARTS for part in rel_path.parts):
            continue

        # Return relative paths (matching TS behavior)
        filtered.append(str(rel_path))

    return sorted(filtered)[:limit]
