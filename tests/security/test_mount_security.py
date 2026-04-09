"""Tests for rolemesh.mount_security."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from rolemesh.core.types import AdditionalMount
from rolemesh.security.mount_security import (
    DEFAULT_BLOCKED_PATTERNS,
    generate_allowlist_template,
    load_mount_allowlist,
    reset_cache,
    validate_mount,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_load_mount_allowlist_missing(tmp_path: Path) -> None:
    reset_cache()
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", tmp_path / "nonexistent.json"):
        result = load_mount_allowlist()
    assert result is None
    reset_cache()


def test_load_mount_allowlist_valid(tmp_path: Path) -> None:
    reset_cache()
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(tmp_path), "allowReadWrite": True}],
                "blockedPatterns": ["custom_blocked"],
                "nonMainReadOnly": True,
            }
        )
    )
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        result = load_mount_allowlist()
    assert result is not None
    assert len(result.allowed_roots) == 1
    assert "custom_blocked" in result.blocked_patterns
    for pattern in DEFAULT_BLOCKED_PATTERNS:
        assert pattern in result.blocked_patterns
    reset_cache()


def test_validate_mount_no_allowlist(tmp_path: Path) -> None:
    reset_cache()
    mount = AdditionalMount(host_path=str(tmp_path))
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", tmp_path / "nonexistent.json"):
        result = validate_mount(mount, is_super_agent=True)
    assert result.allowed is False
    assert "No mount allowlist" in result.reason
    reset_cache()


def test_validate_mount_invalid_container_path() -> None:
    reset_cache()
    mount = AdditionalMount(host_path="/tmp/test", container_path="../escape")
    with patch("rolemesh.security.mount_security.load_mount_allowlist") as mock_load:
        from rolemesh.core.types import MountAllowlist

        mock_load.return_value = MountAllowlist(
            allowed_roots=[],
            blocked_patterns=[],
            non_main_read_only=True,
        )
        result = validate_mount(mount, is_super_agent=True)
    assert result.allowed is False
    assert ".." in result.reason
    reset_cache()


def test_validate_mount_blocked_pattern(tmp_path: Path) -> None:
    reset_cache()
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(tmp_path), "allowReadWrite": True}],
                "blockedPatterns": [],
                "nonMainReadOnly": False,
            }
        )
    )
    mount = AdditionalMount(host_path=str(ssh_dir))
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        result = validate_mount(mount, is_super_agent=True)
    assert result.allowed is False
    assert "blocked pattern" in result.reason
    reset_cache()


def test_validate_mount_not_under_allowed_root(tmp_path: Path) -> None:
    reset_cache()
    config_file = tmp_path / "allowlist.json"
    other_dir = tmp_path / "allowed"
    other_dir.mkdir()
    target_dir = tmp_path / "notallowed"
    target_dir.mkdir()
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(other_dir), "allowReadWrite": True}],
                "blockedPatterns": [],
                "nonMainReadOnly": False,
            }
        )
    )
    mount = AdditionalMount(host_path=str(target_dir))
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        result = validate_mount(mount, is_super_agent=True)
    assert result.allowed is False
    assert "not under any allowed root" in result.reason
    reset_cache()


def test_validate_mount_success(tmp_path: Path) -> None:
    reset_cache()
    target_dir = tmp_path / "myproject"
    target_dir.mkdir()
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(tmp_path), "allowReadWrite": True}],
                "blockedPatterns": [],
                "nonMainReadOnly": False,
            }
        )
    )
    mount = AdditionalMount(host_path=str(target_dir), readonly=False)
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        result = validate_mount(mount, is_super_agent=True)
    assert result.allowed is True
    assert result.effective_readonly is False
    reset_cache()


def test_validate_mount_forced_readonly_non_main(tmp_path: Path) -> None:
    reset_cache()
    target_dir = tmp_path / "project"
    target_dir.mkdir()
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(tmp_path), "allowReadWrite": True}],
                "blockedPatterns": [],
                "nonMainReadOnly": True,
            }
        )
    )
    mount = AdditionalMount(host_path=str(target_dir), readonly=False)
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        result = validate_mount(mount, is_super_agent=False)
    assert result.allowed is True
    assert result.effective_readonly is True
    reset_cache()


def test_validate_additional_mounts(tmp_path: Path) -> None:
    reset_cache()
    from rolemesh.security.mount_security import validate_additional_mounts

    target = tmp_path / "proj"
    target.mkdir()
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(tmp_path), "allowReadWrite": True}],
                "blockedPatterns": [],
                "nonMainReadOnly": False,
            }
        )
    )
    mounts = [AdditionalMount(host_path=str(target))]
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        validated = validate_additional_mounts(mounts, "test-group", is_super_agent=True)
    assert len(validated) == 1
    assert "/workspace/extra/" in str(validated[0]["container_path"])
    reset_cache()


def test_validate_mount_nonexistent_path(tmp_path: Path) -> None:
    reset_cache()
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "allowedRoots": [{"path": str(tmp_path), "allowReadWrite": True}],
                "blockedPatterns": [],
                "nonMainReadOnly": False,
            }
        )
    )
    mount = AdditionalMount(host_path=str(tmp_path / "nonexistent"))
    with patch("rolemesh.security.mount_security.MOUNT_ALLOWLIST_PATH", config_file):
        result = validate_mount(mount, is_super_agent=True)
    assert result.allowed is False
    assert "does not exist" in result.reason
    reset_cache()


def test_generate_allowlist_template() -> None:
    template = generate_allowlist_template()
    parsed = json.loads(template)
    assert "allowedRoots" in parsed
    assert "blockedPatterns" in parsed
    assert "nonMainReadOnly" in parsed
