"""Tests for rolemesh.env."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rolemesh.core.env import read_env_file

if TYPE_CHECKING:
    from pathlib import Path


def test_read_env_file_basic(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nBAZ=qux\n")
    result = read_env_file(["FOO", "BAZ"], env_path=env_file)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_read_env_file_only_requested_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nBAZ=qux\n")
    result = read_env_file(["FOO"], env_path=env_file)
    assert result == {"FOO": "bar"}


def test_read_env_file_strips_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=\"hello\"\nB='world'\n")
    result = read_env_file(["A", "B"], env_path=env_file)
    assert result == {"A": "hello", "B": "world"}


def test_read_env_file_skips_comments_and_empty(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\n\nFOO=bar\n")
    result = read_env_file(["FOO"], env_path=env_file)
    assert result == {"FOO": "bar"}


def test_read_env_file_missing_file(tmp_path: Path) -> None:
    result = read_env_file(["FOO"], env_path=tmp_path / "nonexistent")
    assert result == {}


def test_read_env_file_skips_empty_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=\nBAR=baz\n")
    result = read_env_file(["FOO", "BAR"], env_path=env_file)
    assert result == {"BAR": "baz"}
