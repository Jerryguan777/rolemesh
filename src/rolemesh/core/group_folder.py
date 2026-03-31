"""Group folder name validation and path resolution."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rolemesh.core.config import DATA_DIR, GROUPS_DIR

if TYPE_CHECKING:
    from pathlib import Path

_GROUP_FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RESERVED_FOLDERS = frozenset({"global"})


def is_valid_group_folder(folder: str) -> bool:
    """Check if a group folder name is valid."""
    if not folder or folder != folder.strip():
        return False
    if not _GROUP_FOLDER_PATTERN.match(folder):
        return False
    if "/" in folder or "\\" in folder or ".." in folder:
        return False
    return folder.lower() not in _RESERVED_FOLDERS


def assert_valid_group_folder(folder: str) -> None:
    """Raise ValueError if the group folder name is invalid."""
    if not is_valid_group_folder(folder):
        raise ValueError(f'Invalid group folder "{folder}"')


def _ensure_within_base(base_dir: Path, resolved: Path) -> None:
    """Raise ValueError if resolved path escapes base_dir."""
    try:
        resolved.relative_to(base_dir)
    except ValueError:
        raise ValueError(f"Path escapes base directory: {resolved}") from None


def resolve_group_folder_path(folder: str) -> Path:
    """Resolve and validate a group folder to its absolute path."""
    assert_valid_group_folder(folder)
    group_path = (GROUPS_DIR / folder).resolve()
    _ensure_within_base(GROUPS_DIR.resolve(), group_path)
    return group_path


def resolve_group_ipc_path(folder: str) -> Path:
    """Resolve and validate a group IPC directory path."""
    assert_valid_group_folder(folder)
    ipc_base = (DATA_DIR / "ipc").resolve()
    ipc_path = (ipc_base / folder).resolve()
    _ensure_within_base(ipc_base, ipc_path)
    return ipc_path
