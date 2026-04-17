"""Git utilities — Python port of packages/coding-agent/src/utils/git.ts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# GitSource dataclass
# ---------------------------------------------------------------------------


@dataclass
class GitSource:
    """Represents a parsed Git source URL."""

    type: Literal["git"] = "git"
    repo: str = ""
    host: str = ""
    path: str = ""
    ref: str | None = None
    pinned: bool = False


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

# Accepted prefixes for explicit git: scheme
_GIT_PREFIX = "git:"

# Pattern for bare host/path or https:// URLs (after scheme has been stripped)
# Examples:
#   github.com/owner/repo
#   github.com/owner/repo@main
_HOST_PATH_RE = re.compile(
    r"^(?P<host>[a-zA-Z0-9._-]+\.[a-zA-Z]{2,})"  # hostname (must contain a dot + TLD)
    r"/"
    r"(?P<path>[a-zA-Z0-9._\-/]+?)"  # owner/repo path
    r"(?:\.git)?"  # optional .git suffix
    r"(?:#(?P<ref>[^@#\s]+))?"  # optional #ref
    r"(?:@(?P<pinned_ref>[^@#\s]+))?$"  # optional @ref (pinned)
)

_SSH_RE = re.compile(
    r"^(?:git@)?"  # optional git@ prefix
    r"(?P<host>[a-zA-Z0-9._-]+)"  # hostname
    r":"
    r"(?P<path>[a-zA-Z0-9._\-/]+?)"  # owner/repo path
    r"(?:\.git)?"  # optional .git suffix
    r"(?:#(?P<ref>[^@#\s]+))?"  # optional #ref
    r"(?:@(?P<pinned_ref>[^@#\s]+))?$"  # optional @ref (pinned)
)


def parse_git_url(source: str) -> GitSource | None:
    """Parse a git URL string into a GitSource.

    Accepts the following forms:
    - ``github.com/owner/repo``
    - ``https://github.com/owner/repo``
    - ``git@github.com:owner/repo.git``
    - ``git:github.com/owner/repo@main``
    - Any of the above with an optional ``@ref`` suffix for pinning.

    Returns ``None`` if the string cannot be parsed as a git URL.
    """
    raw = source.strip()

    # Strip explicit "git:" prefix if present (it's just a marker)
    if raw.startswith(_GIT_PREFIX):
        raw = raw[len(_GIT_PREFIX) :]

    # Strip https:// or http:// scheme
    if raw.startswith("https://"):
        raw = raw[len("https://") :]
    elif raw.startswith("http://"):
        raw = raw[len("http://") :]

    # Try SSH syntax first: git@host:path or host:path (colon before any slash)
    colon_pos = raw.find(":")
    slash_pos = raw.find("/")
    if colon_pos != -1 and (slash_pos == -1 or colon_pos < slash_pos):
        m = _SSH_RE.match(raw)
        if m:
            return _build_source(m)

    # Try bare host/path syntax
    m = _HOST_PATH_RE.match(raw)
    if m:
        return _build_source(m)

    return None


def _build_source(m: re.Match[str]) -> GitSource:
    host = m.group("host")
    path = m.group("path").rstrip("/")
    ref_hash = m.group("ref")
    ref_at = m.group("pinned_ref")

    ref: str | None = None
    pinned = False
    if ref_at is not None:
        ref = ref_at
        pinned = True
    elif ref_hash is not None:
        ref = ref_hash
        pinned = False

    # Build the full clone URL (matches TS GitSource.repo semantics)
    clone_url = f"https://{host}/{path}"

    return GitSource(
        type="git",
        repo=clone_url,
        host=host,
        path=path,
        ref=ref,
        pinned=pinned,
    )


__all__ = [
    "GitSource",
    "parse_git_url",
]
