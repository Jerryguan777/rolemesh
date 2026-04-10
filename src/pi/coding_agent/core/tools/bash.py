"""Bash tool — Python port of packages/coding-agent/src/core/tools/bash.ts."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pi.agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback
from pi.ai.types import TextContent

from .truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, TruncationResult, format_size, truncate_tail


@dataclass
class BashToolInput:
    """Input parameters for the bash tool."""

    command: str
    timeout: float | None = None


@dataclass
class BashToolDetails:
    """Details returned by the bash tool."""

    truncation: TruncationResult | None = None
    full_output_path: str | None = None


@dataclass
class BashSpawnContext:
    """Context for bash command execution."""

    command: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)


BashSpawnHook = Callable[[BashSpawnContext], BashSpawnContext]
"""Hook to adjust command, cwd, or env before execution."""


@dataclass
class BashToolOptions:
    """Options for the bash tool."""

    command_prefix: str | None = None
    """Command prefix prepended to every command."""
    spawn_hook: BashSpawnHook | None = None
    """Hook to adjust command, cwd, or env before execution."""


# Rolling buffer window: keep at most 2x DEFAULT_MAX_BYTES of recent chunks in memory
MAX_CHUNKS_BYTES = DEFAULT_MAX_BYTES * 2


class BashTool(AgentTool):
    """Execute bash commands in a subprocess."""

    def __init__(self, cwd: str) -> None:
        self._cwd = cwd

    @property
    def name(self) -> str:
        return "bash"

    @property
    def label(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return (
            f"Execute a bash command. Output is truncated to {DEFAULT_MAX_LINES} lines "
            f"or {DEFAULT_MAX_BYTES // 1024}KB, showing the tail. "
            "Use timeout to limit long-running commands."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (optional)",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Execute a bash command and return the output."""
        command: str = params["command"]
        timeout: float | None = params.get("timeout")

        if not os.path.isdir(self._cwd):
            raise RuntimeError(f"cwd does not exist or is not a directory: {self._cwd}")

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )

        # Rolling window buffer: bounded to MAX_CHUNKS_BYTES of recent output
        chunks: list[bytes] = []
        chunks_bytes = 0
        total_bytes = 0
        temp_file_path: str | None = None
        temp_fd: int | None = None

        async def collect_output() -> None:
            nonlocal chunks_bytes, total_bytes, temp_file_path, temp_fd

            async def read_stream(stream: asyncio.StreamReader) -> None:
                nonlocal chunks_bytes, total_bytes, temp_file_path, temp_fd
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    chunks_bytes += len(chunk)
                    total_bytes += len(chunk)

                    # Once output exceeds max, write everything to a temp file
                    if total_bytes > DEFAULT_MAX_BYTES and temp_fd is None:
                        temp_fd, temp_file_path = tempfile.mkstemp(prefix="pi-bash-", suffix=".log")
                        # Write all buffered chunks so far
                        for c in chunks:
                            os.write(temp_fd, c)
                    elif temp_fd is not None:
                        os.write(temp_fd, chunk)

                    # Trim oldest chunks to keep memory bounded
                    while chunks_bytes > MAX_CHUNKS_BYTES and len(chunks) > 1:
                        removed = chunks.pop(0)
                        chunks_bytes -= len(removed)

                    if on_update:
                        text = b"".join(chunks).decode("utf-8", errors="replace")
                        trunc = truncate_tail(text)
                        on_update(
                            AgentToolResult(
                                content=[TextContent(type="text", text=trunc.content or "")],
                                details=None,
                            )
                        )

            if proc.stdout is None or proc.stderr is None:
                raise RuntimeError("subprocess streams unavailable")
            await asyncio.gather(read_stream(proc.stdout), read_stream(proc.stderr))
            await proc.wait()

        collect_task: asyncio.Task[None] = asyncio.create_task(collect_output())

        waiters: list[asyncio.Task[None] | asyncio.Task[bool]] = [collect_task]
        signal_task: asyncio.Task[bool] | None = None
        if signal is not None:
            signal_task = asyncio.create_task(signal.wait())
            waiters.append(signal_task)

        done, _ = await asyncio.wait(
            waiters,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        aborted = signal_task is not None and signal_task in done
        timed_out = not aborted and collect_task not in done

        if aborted or timed_out:
            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()
            collect_task.cancel()
            if signal_task is not None:
                signal_task.cancel()
            if temp_fd is not None:
                with contextlib.suppress(Exception):
                    os.close(temp_fd)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await collect_task

            raw_output = b"".join(chunks).decode("utf-8", errors="replace")
            prefix = raw_output + "\n\n" if raw_output else ""
            if aborted:
                raise RuntimeError(f"{prefix}Command aborted")
            else:
                raise RuntimeError(f"{prefix}Command timed out after {int(timeout or 0)} seconds")

        if signal_task is not None:
            signal_task.cancel()

        # Close temp file fd before reading
        if temp_fd is not None:
            with contextlib.suppress(Exception):
                os.close(temp_fd)

        # Read full output: from temp file if it exists, otherwise from chunks
        if temp_file_path is not None:
            with open(temp_file_path, "rb") as f:
                full_output = f.read().decode("utf-8", errors="replace")
        else:
            full_output = b"".join(chunks).decode("utf-8", errors="replace")

        trunc = truncate_tail(full_output)
        output_text = trunc.content or "(no output)"

        if trunc.truncated:
            start_line = trunc.total_lines - trunc.output_lines + 1
            end_line = trunc.total_lines
            if trunc.last_line_partial:
                last_line_bytes = len((full_output.split("\n") or [""])[-1].encode("utf-8"))
                output_text += (
                    f"\n\n[Showing last {format_size(trunc.output_bytes)} of line {end_line}"
                    f" (line is {format_size(last_line_bytes)}). Full output: {temp_file_path}]"
                )
            elif trunc.truncated_by == "lines":
                output_text += (
                    f"\n\n[Showing lines {start_line}-{end_line} of {trunc.total_lines}. Full output: {temp_file_path}]"
                )
            else:
                output_text += (
                    f"\n\n[Showing lines {start_line}-{end_line} of {trunc.total_lines}"
                    f" ({format_size(DEFAULT_MAX_BYTES)} limit). Full output: {temp_file_path}]"
                )

        details: dict[str, object] = {}
        if trunc.truncated:
            details = {"truncation": trunc, "full_output_path": temp_file_path}

        exit_code = proc.returncode
        if exit_code not in (0, None):
            output_text += f"\n\nCommand exited with code {exit_code}"
            raise RuntimeError(output_text)

        return AgentToolResult(
            content=[TextContent(type="text", text=output_text)],
            details=details if details else None,
        )
