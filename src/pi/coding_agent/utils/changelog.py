"""Changelog parsing utilities.

Python port of packages/coding-agent/src/utils/changelog.ts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ChangelogEntry:
    """A parsed changelog version entry."""

    major: int
    minor: int
    patch: int
    content: str


def parse_changelog(changelog_path: str) -> list[ChangelogEntry]:
    """Parse changelog entries from a CHANGELOG.md file.

    Scans for ## lines containing version numbers and collects content
    until the next ## or EOF.
    """
    path = Path(changelog_path)
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not parse changelog: %s", e)
        return []

    lines = text.split("\n")
    entries: list[ChangelogEntry] = []
    current_lines: list[str] = []
    current_version: dict[str, int] | None = None

    version_re = re.compile(r"##\s+\[?(\d+)\.(\d+)\.(\d+)\]?")

    for line in lines:
        if line.startswith("## "):
            # Save previous entry if exists
            if current_version is not None and current_lines:
                entries.append(
                    ChangelogEntry(
                        major=current_version["major"],
                        minor=current_version["minor"],
                        patch=current_version["patch"],
                        content="\n".join(current_lines).strip(),
                    )
                )

            # Try to parse version from this line
            m = version_re.match(line)
            if m:
                current_version = {
                    "major": int(m.group(1)),
                    "minor": int(m.group(2)),
                    "patch": int(m.group(3)),
                }
                current_lines = [line]
            else:
                current_version = None
                current_lines = []
        elif current_version is not None:
            current_lines.append(line)

    # Save last entry
    if current_version is not None and current_lines:
        entries.append(
            ChangelogEntry(
                major=current_version["major"],
                minor=current_version["minor"],
                patch=current_version["patch"],
                content="\n".join(current_lines).strip(),
            )
        )

    return entries


def compare_versions(v1: ChangelogEntry, v2: ChangelogEntry) -> int:
    """Compare two version entries.

    Returns: negative if v1 < v2, 0 if equal, positive if v1 > v2.
    """
    if v1.major != v2.major:
        return v1.major - v2.major
    if v1.minor != v2.minor:
        return v1.minor - v2.minor
    return v1.patch - v2.patch


def get_new_entries(entries: list[ChangelogEntry], last_version: str) -> list[ChangelogEntry]:
    """Get entries newer than last_version (e.g. '1.2.3')."""
    parts = last_version.split(".")
    last = ChangelogEntry(
        major=int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0,
        minor=int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
        patch=int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        content="",
    )
    return [entry for entry in entries if compare_versions(entry, last) > 0]
