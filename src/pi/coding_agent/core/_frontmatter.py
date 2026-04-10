"""Shared YAML frontmatter parsing utility.

Port of packages/coding-agent/src/utils/frontmatter.ts.
Used by skills.py, prompt_templates.py, and any future modules needing frontmatter parsing.
"""

from __future__ import annotations

from typing import Any


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body_text).
    Normalises CRLF line endings before parsing.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")

    if not normalized.startswith("---"):
        return {}, normalized

    end_idx = normalized.find("\n---", 3)
    if end_idx == -1:
        return {}, normalized

    frontmatter_text = normalized[3:end_idx].strip()
    body = normalized[end_idx + 4 :].lstrip("\n")

    try:
        import yaml  # type: ignore[import-untyped]

        fm = yaml.safe_load(frontmatter_text)
        if isinstance(fm, dict):
            return fm, body
    except Exception:
        pass

    # Fallback: basic key: value parsing (no yaml dependency)
    fm_dict: dict[str, Any] = {}
    for line in frontmatter_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            fm_dict[key.strip()] = value.strip()

    return fm_dict, body


def strip_frontmatter(content: str) -> str:
    """Strip YAML frontmatter and return the body only."""
    _, body = parse_frontmatter(content)
    return body
