"""Path utilities — Python port of packages/coding-agent/src/core/tools/path-utils.ts."""

from __future__ import annotations

import os
import unicodedata

# Unicode spaces that should be normalized to regular spaces
_UNICODE_SPACES = "\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"


def expand_path(file_path: str) -> str:
    """Expand a file path: strip @ prefix, normalize unicode spaces, expand ~."""
    # Strip a single leading "@" prefix (used by some LLMs to reference files)
    path = file_path[1:] if file_path.startswith("@") else file_path

    # Normalize unicode spaces to regular spaces
    for ch in _UNICODE_SPACES:
        path = path.replace(ch, " ")

    # Expand ~ to home directory
    path = os.path.expanduser(path)

    return path


def resolve_to_cwd(file_path: str, cwd: str) -> str:
    """Resolve a file path relative to cwd. Absolute paths are returned as-is."""
    expanded = expand_path(file_path)
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(cwd, expanded))


def resolve_read_path(file_path: str, cwd: str) -> str:
    """Resolve a file path for reading, trying multiple variants if not found.

    Tries: basic resolution, NFD normalization, curly-quote variants.
    Returns the first path that exists, or the basic resolved path as fallback.
    """
    base = resolve_to_cwd(file_path, cwd)

    # Try the basic path first
    if os.path.exists(base):
        return base

    # Try NFD normalization (macOS sometimes stores filenames in NFD)
    nfd = unicodedata.normalize("NFD", base)
    if nfd != base and os.path.exists(nfd):
        return nfd

    # Try NFC normalization
    nfc = unicodedata.normalize("NFC", base)
    if nfc != base and os.path.exists(nfc):
        return nfc

    # Try replacing curly quotes with straight quotes
    curly_variant = base.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    if curly_variant != base and os.path.exists(curly_variant):
        return curly_variant

    # Return the basic path as fallback (caller will handle missing file)
    return base
