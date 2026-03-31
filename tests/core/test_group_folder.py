"""Tests for rolemesh.group_folder."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from rolemesh.core.group_folder import (
    assert_valid_group_folder,
    is_valid_group_folder,
    resolve_group_folder_path,
    resolve_group_ipc_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_valid_folder_names() -> None:
    assert is_valid_group_folder("test") is True
    assert is_valid_group_folder("my-group") is True
    assert is_valid_group_folder("Group_123") is True
    assert is_valid_group_folder("A") is True


def test_invalid_folder_names() -> None:
    assert is_valid_group_folder("") is False
    assert is_valid_group_folder(" test") is False
    assert is_valid_group_folder("test ") is False
    assert is_valid_group_folder("-start") is False
    assert is_valid_group_folder("_start") is False
    assert is_valid_group_folder("a/b") is False
    assert is_valid_group_folder("a\\b") is False
    assert is_valid_group_folder("a..b") is False
    assert is_valid_group_folder("global") is False
    assert is_valid_group_folder("Global") is False


def test_too_long_name() -> None:
    assert is_valid_group_folder("a" * 64) is True
    assert is_valid_group_folder("a" * 65) is False


def test_assert_valid_raises() -> None:
    with pytest.raises(ValueError, match="Invalid group folder"):
        assert_valid_group_folder("")

    with pytest.raises(ValueError, match="Invalid group folder"):
        assert_valid_group_folder("global")


def test_resolve_group_folder_path(tmp_path: Path) -> None:
    groups_dir = tmp_path / "groups"
    groups_dir.mkdir()
    with patch("rolemesh.core.group_folder.GROUPS_DIR", groups_dir):
        result = resolve_group_folder_path("mygroup")
    assert result.name == "mygroup"
    assert str(groups_dir) in str(result)


def test_resolve_group_ipc_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with patch("rolemesh.core.group_folder.DATA_DIR", data_dir):
        result = resolve_group_ipc_path("mygroup")
    assert result.name == "mygroup"


def test_resolve_rejects_invalid_folder() -> None:
    with pytest.raises(ValueError):
        resolve_group_folder_path("../escape")

    with pytest.raises(ValueError):
        resolve_group_ipc_path("")
