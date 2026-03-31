"""Core type definitions for RoleMesh."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Security / container mount types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Multi-tenant data model
# ---------------------------------------------------------------------------


@dataclass
class Tenant:
    """An organization or tenant."""

    id: str  # UUID
    name: str
    slug: str | None = None
    plan: str | None = None
    max_concurrent_containers: int = 5
    last_message_cursor: str | None = None  # TIMESTAMPTZ iso
    created_at: str = ""


@dataclass
class User:
    """A user within a tenant (reserved for future permission control)."""

    id: str  # UUID
    tenant_id: str
    name: str
    email: str | None = None
    role: str = "member"  # admin / manager / member
    channel_ids: dict[str, str] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class Coworker:
    """An AI coworker with own workspace, identity, and agent config."""

    id: str  # UUID
    tenant_id: str
    name: str
    folder: str
    agent_backend: str = "claude-code"
    system_prompt: str | None = None
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    is_admin: bool = False
    container_config: ContainerConfig | None = None
    max_concurrent: int = 2
    status: str = "active"
    created_at: str = ""


@dataclass
class ChannelBinding:
    """Per-coworker per-channel-type bot credentials."""

    id: str  # UUID
    coworker_id: str
    tenant_id: str
    channel_type: str  # "telegram" / "slack" / "web"
    credentials: dict[str, str] = field(default_factory=dict)
    bot_display_name: str | None = None
    status: str = "active"
    created_at: str = ""


@dataclass
class Conversation:
    """A conversation context: per-coworker per-chat."""

    id: str  # UUID
    tenant_id: str
    coworker_id: str
    channel_binding_id: str
    channel_chat_id: str  # tg group ID / slack channel ID
    name: str | None = None
    requires_trigger: bool = True
    last_agent_invocation: str | None = None
    created_at: str = ""


# ---------------------------------------------------------------------------
# Legacy types (backward compatibility — deprecated, will be removed)
# ---------------------------------------------------------------------------


@dataclass
class RegisteredGroup:
    """A registered group with its configuration.

    DEPRECATED: Use Coworker + ChannelBinding + Conversation instead.
    Kept for backward compatibility during migration.
    """

    name: str
    folder: str
    trigger: str
    added_at: str
    container_config: ContainerConfig | None = None
    requires_trigger: bool = True
    is_main: bool = False


def registered_group_to_coworker(
    group: RegisteredGroup,
    tenant_id: str,
    coworker_id: str,
) -> Coworker:
    """Convert a legacy RegisteredGroup to a Coworker."""
    return Coworker(
        id=coworker_id,
        tenant_id=tenant_id,
        name=group.name,
        folder=group.folder,
        is_admin=group.is_main,
        container_config=group.container_config,
    )


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


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
    tenant_id: str
    coworker_id: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["group", "isolated"]
    conversation_id: str | None = None
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str = ""
    # Legacy compat fields (deprecated)
    group_folder: str = ""
    chat_jid: str = ""


@dataclass(frozen=True)
class TaskRunLog:
    """Log entry for a task execution."""

    tenant_id: str
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
