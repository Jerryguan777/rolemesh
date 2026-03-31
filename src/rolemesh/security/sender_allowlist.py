"""Per-chat sender filtering and trigger authorization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

from rolemesh.core.config import SENDER_ALLOWLIST_PATH
from rolemesh.core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class ChatAllowlistEntry:
    """Allowlist entry for a specific chat."""

    allow: str | list[str]  # "*" or list of sender identifiers
    mode: Literal["trigger", "drop"] = "trigger"


@dataclass(frozen=True)
class SenderAllowlistConfig:
    """Complete sender allowlist configuration."""

    default: ChatAllowlistEntry = field(default_factory=lambda: ChatAllowlistEntry(allow="*", mode="trigger"))
    chats: dict[str, ChatAllowlistEntry] = field(default_factory=dict)
    log_denied: bool = True


_DEFAULT_CONFIG = SenderAllowlistConfig()


def _is_valid_entry(entry: object) -> bool:
    """Check if an entry dict is a valid ChatAllowlistEntry."""
    if not isinstance(entry, dict):
        return False
    allow = entry.get("allow")
    valid_allow = allow == "*" or (isinstance(allow, list) and all(isinstance(v, str) for v in allow))
    valid_mode = entry.get("mode") in ("trigger", "drop")
    return bool(valid_allow and valid_mode)


def _parse_entry(raw: dict[str, object]) -> ChatAllowlistEntry:
    """Parse a raw dict into a ChatAllowlistEntry."""
    return ChatAllowlistEntry(
        allow=raw["allow"],  # type: ignore[arg-type]
        mode=raw["mode"],  # type: ignore[arg-type]
    )


def load_sender_allowlist(path_override: Path | None = None) -> SenderAllowlistConfig:
    """Load sender allowlist from config file."""
    file_path = path_override or SENDER_ALLOWLIST_PATH

    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _DEFAULT_CONFIG
    except OSError:
        logger.warning("sender-allowlist: cannot read config", path=str(file_path))
        return _DEFAULT_CONFIG

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("sender-allowlist: invalid JSON", path=str(file_path))
        return _DEFAULT_CONFIG

    if not isinstance(parsed, dict):
        return _DEFAULT_CONFIG

    if not _is_valid_entry(parsed.get("default", {})):
        logger.warning("sender-allowlist: invalid or missing default entry", path=str(file_path))
        return _DEFAULT_CONFIG

    chats: dict[str, ChatAllowlistEntry] = {}
    raw_chats = parsed.get("chats")
    if isinstance(raw_chats, dict):
        for jid, entry in raw_chats.items():
            if _is_valid_entry(entry):
                chats[jid] = _parse_entry(entry)
            else:
                logger.warning("sender-allowlist: skipping invalid chat entry", jid=jid, path=str(file_path))

    return SenderAllowlistConfig(
        default=_parse_entry(parsed["default"]),
        chats=chats,
        log_denied=parsed.get("logDenied", True) is not False,
    )


def _get_entry(chat_jid: str, cfg: SenderAllowlistConfig) -> ChatAllowlistEntry:
    """Get the allowlist entry for a chat, falling back to default."""
    return cfg.chats.get(chat_jid, cfg.default)


def is_sender_allowed(chat_jid: str, sender: str, cfg: SenderAllowlistConfig) -> bool:
    """Check if a sender is allowed in a chat."""
    entry = _get_entry(chat_jid, cfg)
    if entry.allow == "*":
        return True
    return isinstance(entry.allow, list) and sender in entry.allow


def should_drop_message(chat_jid: str, cfg: SenderAllowlistConfig) -> bool:
    """Check if messages from non-allowed senders should be dropped."""
    return _get_entry(chat_jid, cfg).mode == "drop"


def is_trigger_allowed(chat_jid: str, sender: str, cfg: SenderAllowlistConfig) -> bool:
    """Check if a sender is allowed to trigger the agent."""
    allowed = is_sender_allowed(chat_jid, sender, cfg)
    if not allowed and cfg.log_denied:
        logger.debug("sender-allowlist: trigger denied for sender", chat_jid=chat_jid, sender=sender)
    return allowed
