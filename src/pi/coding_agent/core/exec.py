"""Shell command execution utilities — Python port of packages/coding-agent/src/core/exec.ts."""

from __future__ import annotations

import asyncio
import contextlib
import signal as _signal
from dataclasses import dataclass


@dataclass
class ExecOptions:
    """Options for executing shell commands."""

    signal: asyncio.Event | None = None
    timeout: float | None = None  # seconds (TS uses milliseconds)
    cwd: str | None = None


@dataclass
class ExecResult:
    """Result of executing a shell command."""

    stdout: str = ""
    stderr: str = ""
    code: int = 0
    killed: bool = False


async def exec_command(
    command: str,
    args: list[str],
    cwd: str,
    options: ExecOptions | None = None,
) -> ExecResult:
    """Execute a shell command and return stdout/stderr/code.

    Supports timeout and abort via asyncio.Event.

    Args:
        command: Executable path or name.
        args: Command-line arguments.
        cwd: Working directory.
        options: Optional ExecOptions for timeout and cancellation.

    Returns:
        ExecResult with captured stdout, stderr, exit code, and kill status.
    """
    opts = options or ExecOptions()

    # Check if already cancelled
    if opts.signal is not None and opts.signal.is_set():
        return ExecResult(stdout="", stderr="", code=1, killed=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return ExecResult(stdout="", stderr=str(exc), code=1, killed=False)

    killed = False

    async def _kill() -> None:
        nonlocal killed
        killed = True
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(_signal.SIGTERM)
        await asyncio.sleep(5)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def _wait_for_signal() -> None:
        """Wait until the abort event is set, then kill the process."""
        if opts.signal is None:
            return
        await opts.signal.wait()
        await _kill()

    # Build coroutines to await concurrently
    wait_coro = proc.communicate()

    tasks: list[asyncio.Task[object]] = []

    if opts.signal is not None:
        signal_task: asyncio.Task[None] = asyncio.create_task(_wait_for_signal())
        tasks.append(signal_task)

    try:
        if opts.timeout is not None and opts.timeout > 0:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(wait_coro, timeout=opts.timeout)
            except TimeoutError:
                await _kill()
                # After killing, drain any remaining pipe data with a bounded timeout.
                # We create a fresh read rather than reusing the cancelled coroutine to
                # avoid pipe state issues from the cancelled wait_for internals.
                try:
                    out = await asyncio.wait_for(proc.stdout.read(), timeout=5.0) if proc.stdout else b""
                    err = await asyncio.wait_for(proc.stderr.read(), timeout=5.0) if proc.stderr else b""
                except TimeoutError:
                    out, err = b"", b""
                await proc.wait()
                stdout_bytes, stderr_bytes = out, err
        else:
            stdout_bytes, stderr_bytes = await wait_coro
    finally:
        for t in tasks:
            t.cancel()

    code = proc.returncode if proc.returncode is not None else 0
    return ExecResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        code=code,
        killed=killed,
    )
