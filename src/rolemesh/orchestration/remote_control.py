"""Claude Remote Control session management.

Spawns `claude remote-control` as a detached process, polls its stdout
file for the session URL, and manages the session lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rolemesh.core.config import DATA_DIR
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger()


@dataclass
class RemoteControlSession:
    """Active remote control session state."""

    pid: int
    url: str
    started_by: str
    started_in_chat: str
    started_at: str


_active_session: RemoteControlSession | None = None

_URL_REGEX = re.compile(r"https://claude\.ai/code\S+")
_URL_TIMEOUT_S: float = 30.0
_URL_POLL_S: float = 0.2
_STATE_FILE: Path = DATA_DIR / "remote-control.json"
_STDOUT_FILE: Path = DATA_DIR / "remote-control.stdout"
_STDERR_FILE: Path = DATA_DIR / "remote-control.stderr"


def _save_state(session: RemoteControlSession) -> None:
    """Persist session state to disk."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(
            {
                "pid": session.pid,
                "url": session.url,
                "startedBy": session.started_by,
                "startedInChat": session.started_in_chat,
                "startedAt": session.started_at,
            }
        ),
        encoding="utf-8",
    )


def _clear_state() -> None:
    """Remove session state file."""
    with contextlib.suppress(FileNotFoundError):
        _STATE_FILE.unlink()


def _is_process_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def restore_remote_control() -> None:
    """Restore session from disk on startup.

    If the process is still alive, adopt it. Otherwise, clean up.
    """
    global _active_session

    try:
        data = _STATE_FILE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return

    try:
        raw = json.loads(data)
        session = RemoteControlSession(
            pid=raw["pid"],
            url=raw["url"],
            started_by=raw["startedBy"],
            started_in_chat=raw["startedInChat"],
            started_at=raw["startedAt"],
        )
        if session.pid and _is_process_alive(session.pid):
            _active_session = session
            logger.info(
                "Restored Remote Control session from previous run",
                pid=session.pid,
                url=session.url,
            )
        else:
            _clear_state()
    except (json.JSONDecodeError, KeyError, TypeError):
        _clear_state()


def get_active_session() -> RemoteControlSession | None:
    """Return the active remote control session, or None."""
    return _active_session


def _reset_for_testing() -> None:
    """Reset module state for tests."""
    global _active_session
    _active_session = None


def _get_state_file_path() -> Path:
    """Return the state file path (for tests)."""
    return _STATE_FILE


async def start_remote_control(
    sender: str,
    chat_jid: str,
    cwd: str,
) -> dict[str, object]:
    """Start a remote control session.

    Returns {"ok": True, "url": str} on success,
    or {"ok": False, "error": str} on failure.
    """
    global _active_session

    if _active_session is not None:
        # Verify the process is still alive
        if _is_process_alive(_active_session.pid):
            return {"ok": True, "url": _active_session.url}
        # Process died - clean up and start a new one
        _active_session = None
        _clear_state()

    # Redirect stdout/stderr to files so the process has no pipes to the parent.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stdout_fd = os.open(str(_STDOUT_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    stderr_fd = os.open(str(_STDERR_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "remote-control",
            "--name",
            "RoleMesh Remote",
            stdin=asyncio.subprocess.PIPE,
            stdout=stdout_fd,
            stderr=stderr_fd,
            cwd=cwd,
            start_new_session=True,  # detached
        )
    except OSError as exc:
        os.close(stdout_fd)
        os.close(stderr_fd)
        return {"ok": False, "error": f"Failed to start: {exc}"}

    # Auto-accept the "Enable Remote Control?" prompt
    if proc.stdin is not None:
        proc.stdin.write(b"y\n")
        proc.stdin.close()

    # Close FDs in the parent - the child inherited copies
    os.close(stdout_fd)
    os.close(stderr_fd)

    pid = proc.pid
    if pid is None:
        return {"ok": False, "error": "Failed to get process PID"}

    # Poll the stdout file for the URL
    start_time = asyncio.get_event_loop().time()

    while True:
        # Check if process died
        if not _is_process_alive(pid):
            return {"ok": False, "error": "Process exited before producing URL"}

        # Check for URL in stdout file
        content = ""
        with contextlib.suppress(OSError):
            content = _STDOUT_FILE.read_text(encoding="utf-8")

        match = _URL_REGEX.search(content)
        if match:
            session = RemoteControlSession(
                pid=pid,
                url=match.group(0),
                started_by=sender,
                started_in_chat=chat_jid,
                started_at=datetime.now(UTC).isoformat(),
            )
            _active_session = session
            _save_state(session)

            logger.info(
                "Remote Control session started",
                url=match.group(0),
                pid=pid,
                sender=sender,
                chat_jid=chat_jid,
            )
            return {"ok": True, "url": match.group(0)}

        # Timeout check
        if asyncio.get_event_loop().time() - start_time >= _URL_TIMEOUT_S:
            # Try to kill the process group first, then the process
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            return {"ok": False, "error": "Timed out waiting for Remote Control URL"}

        await asyncio.sleep(_URL_POLL_S)


def stop_remote_control() -> dict[str, object]:
    """Stop the active remote control session.

    Returns {"ok": True} on success, or {"ok": False, "error": str}.
    """
    global _active_session

    if _active_session is None:
        return {"ok": False, "error": "No active Remote Control session"}

    pid = _active_session.pid
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)

    _active_session = None
    _clear_state()
    logger.info("Remote Control session stopped", pid=pid)
    return {"ok": True}
