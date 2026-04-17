"""Shell utilities — Python port of packages/coding-agent/src/utils/shell.ts."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

_cached_shell_config: dict[str, Any] | None = None


def get_shell_config() -> dict[str, Any]:
    """Return the shell executable and argument list to use for running commands.

    Prefers bash; falls back to sh if bash is not available.
    Result is cached after the first successful call.
    Returns a dict with keys ``shell`` (str) and ``args`` (list[str]).
    """
    global _cached_shell_config
    if _cached_shell_config is not None:
        return _cached_shell_config
    bash = shutil.which("bash")
    if bash:
        _cached_shell_config = {"shell": bash, "args": ["-c"]}
        return _cached_shell_config
    sh = shutil.which("sh")
    if sh:
        _cached_shell_config = {"shell": sh, "args": ["-c"]}
        return _cached_shell_config
    raise RuntimeError("No suitable shell found (bash or sh)")


def get_shell_env() -> dict[str, str]:
    """Return the process environment with the pi bin directory prepended to PATH.

    Falls back to ``~/.pi/bin`` if the config module is not available.
    """
    env = dict(os.environ)

    # Attempt to import get_bin_dir from a config module; fall back gracefully
    bin_dir: str | None = None
    try:
        from pi.coding_agent.config import get_bin_dir

        bin_dir = str(get_bin_dir())
    except (ImportError, AttributeError):
        fallback = Path.home() / ".pi" / "bin"
        bin_dir = str(fallback)

    existing_path = env.get("PATH", "")
    if bin_dir and bin_dir not in existing_path.split(os.pathsep):
        env["PATH"] = bin_dir + os.pathsep + existing_path
    return env


# Unicode categories to strip during binary output sanitisation.
# Matches control characters (except tab, newline, carriage-return) and
# Unicode format characters that can break terminal rendering.
_SANITISE_PATTERN = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"  # C0 control chars (excluding \t \n \r)
    r"\x80-\x9f"  # C1 control chars
    r"\u200b-\u200f"  # zero-width space / format chars
    r"\u202a-\u202e"  # bidirectional control chars
    r"\u2060-\u206f"  # invisible separators
    r"\ufeff"  # BOM
    r"\ufff9-\uffff"  # specials / surrogates
    r"]"
)


def sanitize_binary_output(text: str) -> str:
    """Remove problematic Unicode characters from binary command output.

    Strips control characters, format characters, and other chars that can
    corrupt terminal rendering or JSON serialisation.
    """
    return _SANITISE_PATTERN.sub("", text)


def kill_process_tree(pid: int) -> None:
    """Kill a process and all of its descendants.

    On POSIX systems this sends SIGKILL to the process group. On non-POSIX
    systems it falls back to a simple ``os.kill``.
    """
    # Try to kill the entire process group (works on Linux/macOS).
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, AttributeError):
        pass

    # Fallback: recursively kill children then the parent.
    try:
        result = subprocess.run(
            ["ps", "-o", "pid", "--no-headers", "--ppid", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for child_pid_str in result.stdout.split():
            with contextlib.suppress(ValueError, ProcessLookupError):
                kill_process_tree(int(child_pid_str))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


__all__ = [
    "get_shell_config",
    "get_shell_env",
    "kill_process_tree",
    "sanitize_binary_output",
]
