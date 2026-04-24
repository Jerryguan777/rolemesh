"""Tests for rolemesh.env."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rolemesh.core.env import read_env_file

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


def test_read_env_file_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing .env + nothing in os.environ → empty result."""
    monkeypatch.delenv("FOO", raising=False)
    result = read_env_file(["FOO"], env_path=tmp_path / "nonexistent")
    assert result == {}


def test_read_env_file_skips_empty_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=\nBAR=baz\n")
    result = read_env_file(["FOO", "BAR"], env_path=env_file)
    assert result == {"BAR": "baz"}


# ---------------------------------------------------------------------------
# os.environ fallback (added post-EC-2)
# ---------------------------------------------------------------------------


def test_falls_back_to_os_environ_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverse-proxy container-deployment path: operator mounts
    secrets via docker --env-file or K8s env, NOT via a .env file.
    read_env_file must pick them up from os.environ or providers
    silently fail to register."""
    monkeypatch.setenv("FOO", "from-env")
    result = read_env_file(["FOO"], env_path=tmp_path / "nonexistent")
    assert result == {"FOO": "from-env"}


def test_file_wins_over_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both a .env and an os.environ value exist, the file wins.
    Matches operator intent: dropping a .env next to the process is
    a deliberate override of ambient config."""
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=from-file\n")
    monkeypatch.setenv("FOO", "from-env")
    result = read_env_file(["FOO"], env_path=env_file)
    assert result == {"FOO": "from-file"}


def test_os_environ_fills_keys_missing_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed source: some keys in .env, some in os.environ. Both should
    be merged — no key silently lost."""
    env_file = tmp_path / ".env"
    env_file.write_text("FROM_FILE=a\n")
    monkeypatch.setenv("FROM_ENV", "b")
    result = read_env_file(["FROM_FILE", "FROM_ENV"], env_path=env_file)
    assert result == {"FROM_FILE": "a", "FROM_ENV": "b"}


def test_empty_os_environ_value_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same ruthlessness as the file path: empty values are not a
    "present" signal. Prevents a misconfigured container that exports
    FOO="" from masking a real .env value or hiding real absence."""
    monkeypatch.setenv("FOO", "")
    result = read_env_file(["FOO"], env_path=tmp_path / "nonexistent")
    assert result == {}
