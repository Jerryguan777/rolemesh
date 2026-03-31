"""Core type definitions for RoleMesh."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class AdditionalMount:
    """Mount configuration for additional directories in containers."""

    host_path: str
    container_path: str | None = None
    readonly: bool = True


@dataclass(frozen=True)
class AllowedRoot:
    """An allowed root directory for mount validation."""

    path: str
    allow_read_write: bool = False
    description: str | None = None


@dataclass(frozen=True)
class MountAllowlist:
    """Security configuration for additional mounts.

    Stored at ~/.config/rolemesh/mount-allowlist.json,
    NOT mounted into any container (tamper-proof from agents).
    """

    allowed_roots: list[AllowedRoot] = field(default_factory=list)
    blocked_patterns: list[str] = field(default_factory=list)
    non_main_read_only: bool = True


@dataclass(frozen=True)
class ContainerConfig:
    """Per-group container configuration."""

    additional_mounts: list[AdditionalMount] = field(default_factory=list)
    timeout: int = 300_000


@dataclass
class RegisteredGroup:
    """A registered group with its configuration."""

    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool = True
    is_main: bool = False


@dataclass(frozen=True)
class NewMessage:
    """An inbound message from a channel."""

    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False
    is_bot_message: bool = False


@dataclass
class ScheduledTask:
    """A scheduled task configuration."""

    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["group", "isolated"]
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str = ""


@dataclass(frozen=True)
class TaskRunLog:
    """Log entry for a task execution."""

    task_id: str
    run_at: str
    duration_ms: int
    status: Literal["success", "error"]
    result: str | None = None
    error: str | None = None


# --- Channel abstraction ---


@runtime_checkable
class Channel(Protocol):
    """Protocol for messaging channel implementations."""

    name: str

    async def connect(self) -> None: ...

    async def send_message(self, jid: str, text: str) -> None: ...

    def is_connected(self) -> bool: ...

    def owns_jid(self, jid: str) -> bool: ...

    async def disconnect(self) -> None: ...


class TypingChannel(Protocol):
    """Channel that supports typing indicators."""

    async def set_typing(self, jid: str, is_typing: bool) -> None: ...


class SyncableChannel(Protocol):
    """Channel that supports group/chat name syncing."""

    async def sync_groups(self, force: bool) -> None: ...


# Callback types
OnInboundMessage = Callable[[str, "NewMessage"], None]
OnChatMetadata = Callable[[str, str, str | None, str | None, bool | None], "Awaitable[None]"]
