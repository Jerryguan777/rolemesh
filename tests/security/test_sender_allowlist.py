"""Tests for rolemesh.sender_allowlist."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rolemesh.security.sender_allowlist import (
    ChatAllowlistEntry,
    SenderAllowlistConfig,
    is_sender_allowed,
    is_trigger_allowed,
    load_sender_allowlist,
    should_drop_message,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_load_default_when_missing(tmp_path: Path) -> None:
    cfg = load_sender_allowlist(tmp_path / "nonexistent.json")
    assert cfg.default.allow == "*"
    assert cfg.default.mode == "trigger"
    assert cfg.log_denied is True


def test_load_valid_config(tmp_path: Path) -> None:
    config_file = tmp_path / "allowlist.json"
    config_file.write_text(
        json.dumps(
            {
                "default": {"allow": "*", "mode": "trigger"},
                "chats": {"chat1": {"allow": ["user1", "user2"], "mode": "drop"}},
                "logDenied": False,
            }
        )
    )
    cfg = load_sender_allowlist(config_file)
    assert cfg.default.allow == "*"
    assert "chat1" in cfg.chats
    assert cfg.chats["chat1"].allow == ["user1", "user2"]
    assert cfg.chats["chat1"].mode == "drop"
    assert cfg.log_denied is False


def test_load_invalid_json(tmp_path: Path) -> None:
    config_file = tmp_path / "bad.json"
    config_file.write_text("not json")
    cfg = load_sender_allowlist(config_file)
    assert cfg.default.allow == "*"


def test_is_sender_allowed_wildcard() -> None:
    cfg = SenderAllowlistConfig()
    assert is_sender_allowed("any_chat", "any_sender", cfg) is True


def test_is_sender_allowed_specific() -> None:
    cfg = SenderAllowlistConfig(
        default=ChatAllowlistEntry(allow=["alice", "bob"], mode="trigger"),
    )
    assert is_sender_allowed("chat1", "alice", cfg) is True
    assert is_sender_allowed("chat1", "charlie", cfg) is False


def test_should_drop_message() -> None:
    cfg = SenderAllowlistConfig(
        default=ChatAllowlistEntry(allow="*", mode="trigger"),
        chats={"drop_chat": ChatAllowlistEntry(allow=["alice"], mode="drop")},
    )
    assert should_drop_message("normal_chat", cfg) is False
    assert should_drop_message("drop_chat", cfg) is True


def test_is_trigger_allowed() -> None:
    cfg = SenderAllowlistConfig(
        default=ChatAllowlistEntry(allow=["alice"], mode="trigger"),
    )
    assert is_trigger_allowed("chat1", "alice", cfg) is True
    assert is_trigger_allowed("chat1", "bob", cfg) is False
