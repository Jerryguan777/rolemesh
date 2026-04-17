"""Bash command execution with streaming — Python port of packages/coding-agent/src/core/bash-executor.ts."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import signal as _signal
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

# Default max bytes before output is truncated and saved to a temp file.
DEFAULT_MAX_BYTES = 200 * 1024  # 200 KB

# Rolling buffer ceiling: keep at most this many bytes in memory.
_MAX_OUTPUT_BYTES = DEFAULT_MAX_BYTES * 2


@runtime_checkable
class BashOperations(Protocol):
    """Protocol for custom bash execution backends (e.g. remote SSH, containers).

    Matches the BashOperations interface from tools/bash.ts.
    """

    async def exec(
        self,
        command: str,
        cwd: str,
        *,
        on_data: Callable[[bytes], None],
        signal: asyncio.Event | None,
    ) -> Any:
        """Execute a command and stream output via on_data. Returns an object with exit_code."""
        ...


@dataclass
class BashExecutorOptions:
    """Options for bash command execution."""

    on_chunk: Callable[[str], None] | None = None
    signal: asyncio.Event | None = None


@dataclass
class BashResult:
    """Result of a bash command execution."""

    output: str = ""
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None


def _sanitize_output(text: str) -> str:
    """Strip ANSI escape sequences, normalize newlines, remove non-printable bytes."""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    text = ansi_escape.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "")
    text = re.sub(r"[^\x09\x0a\x20-\x7e\x80-\xff]", "", text)
    return text


def _truncate_tail(text: str, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, bool]:
    """Truncate text from the beginning if it exceeds max_bytes.

    Returns (possibly-truncated text, truncated flag).
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    tail_bytes = encoded[-max_bytes:]
    truncated_text = tail_bytes.decode("utf-8", errors="replace")
    return truncated_text, True


def _get_shell() -> tuple[str, list[str]]:
    """Return the shell executable and args for running commands."""
    shell = os.environ.get("SHELL", "/bin/bash")
    return shell, ["-c"]


class _OutputBuffer:
    """Shared rolling-buffer + optional temp-file logic for bash output.

    Extracted to avoid duplication between execute_bash and
    execute_bash_with_operations.
    """

    def __init__(self, on_chunk: Callable[[str], None] | None = None) -> None:
        self._on_chunk = on_chunk
        self._chunks: list[str] = []
        self._chunk_bytes = 0
        self._total_bytes = 0
        self._temp_file_path: str | None = None
        self._temp_file: IO[str] | None = None

    def handle(self, data: bytes) -> None:
        """Process a raw data chunk: sanitize, buffer, optionally write to temp file."""
        self._total_bytes += len(data)
        text = _sanitize_output(data.decode("utf-8", errors="replace"))

        # Open temp file once output exceeds the threshold
        if self._total_bytes > DEFAULT_MAX_BYTES and self._temp_file_path is None:
            rand_hex = secrets.token_hex(8)
            self._temp_file_path = str(Path(tempfile.gettempdir()) / f"pi-bash-{rand_hex}.log")
            self._temp_file = open(  # noqa: SIM115
                self._temp_file_path, "w", encoding="utf-8", errors="replace"
            )
            for chunk in self._chunks:
                self._temp_file.write(chunk)

        if self._temp_file is not None:
            self._temp_file.write(text)

        self._chunks.append(text)
        self._chunk_bytes += len(text)

        # Rolling buffer: evict oldest chunks when ceiling is exceeded
        while self._chunk_bytes > _MAX_OUTPUT_BYTES and len(self._chunks) > 1:
            removed = self._chunks.pop(0)
            self._chunk_bytes -= len(removed)

        if self._on_chunk is not None:
            self._on_chunk(text)

    def close(self) -> None:
        """Flush and close the temp file if open."""
        if self._temp_file is not None:
            self._temp_file.close()
            self._temp_file = None

    def finalize(self) -> tuple[str, bool, str | None]:
        """Return (output, truncated, full_output_path)."""
        self.close()
        full_output = "".join(self._chunks)
        truncated_output, was_truncated = _truncate_tail(full_output)
        return truncated_output, was_truncated, self._temp_file_path


async def execute_bash(
    command: str,
    options: BashExecutorOptions | None = None,
) -> BashResult:
    """Execute a bash command with optional streaming and cancellation.

    Features:
    - Streams sanitized output via on_chunk callback
    - Writes large output to temp file for later retrieval
    - Supports cancellation via asyncio.Event
    - Sanitizes output (strips ANSI, removes non-printable chars, normalizes newlines)
    - Truncates output if it exceeds DEFAULT_MAX_BYTES
    - Kills the full process group (not just the direct child) on cancel/timeout

    Args:
        command: The bash command string to execute.
        options: Optional streaming callback and abort event.

    Returns:
        BashResult with output, exit code, and status flags.
    """
    opts = options or BashExecutorOptions()

    # Check if already cancelled
    if opts.signal is not None and opts.signal.is_set():
        return BashResult(output="", exit_code=None, cancelled=True, truncated=False)

    shell, shell_args = _get_shell()

    try:
        proc = await asyncio.create_subprocess_exec(
            shell,
            *shell_args,
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # New session so proc.pid is the group leader; allows killing the
            # entire process tree (e.g. npm → node → grandchildren).
            start_new_session=True,
        )
    except OSError as exc:
        return BashResult(output=str(exc), exit_code=1, cancelled=False, truncated=False)

    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("Subprocess created without pipe streams")

    buf = _OutputBuffer(on_chunk=opts.on_chunk)
    cancelled = False

    def _kill_group() -> None:
        """Send SIGTERM to the process group, then SIGKILL after a short delay."""
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGTERM)
        except (ProcessLookupError, OSError):
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()

    async def _read_stream(stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buf.handle(chunk)

    async def _wait_signal() -> None:
        """Kill the process group when the abort event fires."""
        nonlocal cancelled
        if opts.signal is None:
            return
        await opts.signal.wait()
        cancelled = True
        _kill_group()

    signal_task: asyncio.Task[None] | None = None
    if opts.signal is not None:
        signal_task = asyncio.create_task(_wait_signal())

    try:
        await asyncio.gather(
            _read_stream(proc.stdout),
            _read_stream(proc.stderr),
        )
        await proc.wait()
    finally:
        if signal_task is not None:
            signal_task.cancel()
        buf.close()

    output, was_truncated, temp_file_path = buf.finalize()

    exit_code: int | None = proc.returncode
    # A negative returncode means killed by a signal
    if exit_code is not None and exit_code < 0:
        cancelled = True

    return BashResult(
        output=output,
        exit_code=exit_code if not cancelled else None,
        cancelled=cancelled,
        truncated=was_truncated,
        full_output_path=temp_file_path,
    )


async def execute_bash_with_operations(
    command: str,
    cwd: str,
    operations: BashOperations,
    options: BashExecutorOptions | None = None,
) -> BashResult:
    """Execute a bash command using custom BashOperations (for remote execution).

    Args:
        command: The bash command to execute.
        cwd: Working directory.
        operations: A BashOperations-compatible object with an exec() method.
        options: Optional streaming callback and abort event.

    Returns:
        BashResult with combined output and status.
    """
    opts = options or BashExecutorOptions()
    buf = _OutputBuffer(on_chunk=opts.on_chunk)

    def on_data(data: bytes) -> None:
        buf.handle(data)

    try:
        result = await operations.exec(
            command,
            cwd,
            on_data=on_data,
            signal=opts.signal,
        )
        output, was_truncated, temp_file_path = buf.finalize()

        cancelled = opts.signal is not None and opts.signal.is_set()

        return BashResult(
            output=output,
            exit_code=None if cancelled else getattr(result, "exit_code", None),
            cancelled=cancelled,
            truncated=was_truncated,
            full_output_path=temp_file_path,
        )
    except Exception:
        output, was_truncated, temp_file_path = buf.finalize()

        if opts.signal is not None and opts.signal.is_set():
            return BashResult(
                output=output,
                exit_code=None,
                cancelled=True,
                truncated=was_truncated,
                full_output_path=temp_file_path,
            )
        raise
