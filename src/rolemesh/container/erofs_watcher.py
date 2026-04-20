"""Detect tmpfs-whitelist misses in agent container stderr (Layer 2 defense).

When an agent hits a path that is on the readonly rootfs (not covered by
`_default_tmpfs()` or any bind mount), the kernel returns EROFS and the
Python stdlib surfaces `[Errno 30] Read-only file system: '<path>'`. If
the SDK swallows the exception we still want operators to learn about
the miss so the whitelist can be extended before the next user hits it.

Scope: ONLY `Read-only file system` / `Errno 30`. We explicitly do NOT
match `Permission denied` / `Errno 13` because:
  - `Permission denied` is a valid and *expected* outcome for mount
    security refusals (e.g. agent trying to write /etc/shadow), and
    raising alarms on it would drown out the real signal.
  - A UID-mismatch EACCES on a bind mount is a Docker-config issue, not
    a tmpfs-whitelist issue, and has a different remediation path.
"""

from __future__ import annotations

import re

from rolemesh.core.logger import get_logger

logger = get_logger()

# Matches "Errno 30" with optional whitespace and the literal "Read-only"
# that follows it, so a stray mention of "errno 30" in some other context
# (log noise, NO_SPACE_LEFT etc.) does not trigger false positives.
_EROFS_PATTERN = re.compile(r"Errno\s*30\b.*Read-only|Read-only file system", re.IGNORECASE)

# Python error lines print the offending path in single quotes at the
# end of the message. Example:
#   OSError: [Errno 30] Read-only file system: '/home/agent/.pi'
# We pull the LAST quoted substring so multi-quote messages still land
# on the real target.
_PATH_PATTERN = re.compile(r"'([^']+)'(?:[^']*)$")


class ErofsWatcher:
    """Per-container-invocation watcher that fires an operator-facing
    warning the first time each distinct path triggers EROFS.

    Deduplicates by extracted path so a retry loop doesn't spam the log.
    Not thread-safe — intended to be called from a single stderr reader.
    """

    def __init__(self, *, coworker_name: str, container_name: str) -> None:
        self._coworker = coworker_name
        self._container = container_name
        self._reported: set[str] = set()

    def observe(self, line: str) -> None:
        """Inspect one stderr line; emit at most one warning per unique path."""
        if not _EROFS_PATTERN.search(line):
            return
        m = _PATH_PATTERN.search(line)
        path = m.group(1) if m else "<unknown>"
        if path in self._reported:
            return
        self._reported.add(path)
        # Structured + human-readable. Operators typically wire this to
        # an alerting channel (PagerDuty/Slack) via log-level filters.
        logger.warning(
            "agent hit readonly rootfs — tmpfs whitelist may need update "
            "(see docs/safety/container-hardening.md for how to extend it)",
            path=path,
            coworker=self._coworker,
            container=self._container,
            raw=line[:500],
        )
