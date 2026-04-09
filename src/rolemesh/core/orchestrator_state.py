"""Centralized runtime state for the orchestrator.

Replaces module-level globals (_sessions, _registered_groups, _queue, etc.)
with structured, multi-tenant-aware state objects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.auth.permissions import AgentPermissions
    from rolemesh.core.types import ChannelBinding, Conversation, McpServerConfig, Tenant


@dataclass
class CoworkerConfig:
    """Runtime config loaded from coworkers table."""

    id: str
    tenant_id: str
    name: str
    folder: str
    system_prompt: str | None
    trigger_pattern: re.Pattern[str]
    agent_backend: str
    container_image: str | None
    max_concurrent: int
    role_config: dict[str, object] = field(default_factory=dict)
    tools: list[McpServerConfig] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    agent_role: str = "agent"
    permissions: AgentPermissions | None = None  # filled by __post_init__; always non-None after init

    def __post_init__(self) -> None:
        if self.permissions is None:
            from rolemesh.auth.permissions import AgentPermissions as _AgentPermissions

            self.permissions = _AgentPermissions.for_role(self.agent_role)

    @staticmethod
    def build_trigger_pattern(name: str) -> re.Pattern[str]:
        """Build trigger pattern from coworker name."""
        return re.compile(rf"@{re.escape(name)}\b", re.IGNORECASE)


@dataclass
class ConversationState:
    """Per-conversation runtime state."""

    conversation: Conversation
    session_id: str | None = None
    last_agent_timestamp: str = ""


@dataclass
class CoworkerState:
    """Per-coworker runtime state."""

    config: CoworkerConfig
    conversations: dict[str, ConversationState] = field(default_factory=dict)
    channel_bindings: dict[str, ChannelBinding] = field(default_factory=dict)


class OrchestratorState:
    """All runtime state, structured by tenant and coworker."""

    def __init__(self, global_limit: int = 20) -> None:
        self.tenants: dict[str, Tenant] = {}
        self.coworkers: dict[str, CoworkerState] = {}

        # Three-level scheduling counters
        self.global_active: int = 0
        self.global_limit: int = global_limit
        self.tenant_active: dict[str, int] = {}
        self.coworker_active: dict[str, int] = {}

    def can_start_container(self, tenant_id: str, coworker_id: str) -> bool:
        """Check three-level concurrency limits."""
        if self.global_active >= self.global_limit:
            return False
        tenant = self.tenants.get(tenant_id)
        if tenant and self.tenant_active.get(tenant_id, 0) >= tenant.max_concurrent_containers:
            return False
        cw = self.coworkers.get(coworker_id)
        return not (cw and self.coworker_active.get(coworker_id, 0) >= cw.config.max_concurrent)

    def increment_active(self, tenant_id: str, coworker_id: str) -> None:
        """Increment active container counters at all three levels."""
        self.global_active += 1
        self.tenant_active[tenant_id] = self.tenant_active.get(tenant_id, 0) + 1
        self.coworker_active[coworker_id] = self.coworker_active.get(coworker_id, 0) + 1

    def decrement_active(self, tenant_id: str, coworker_id: str) -> None:
        """Decrement active container counters at all three levels."""
        self.global_active = max(0, self.global_active - 1)
        self.tenant_active[tenant_id] = max(0, self.tenant_active.get(tenant_id, 0) - 1)
        self.coworker_active[coworker_id] = max(0, self.coworker_active.get(coworker_id, 0) - 1)

    def get_coworker_by_folder(self, tenant_id: str, folder: str) -> CoworkerState | None:
        """Find a coworker state by tenant and folder."""
        for cw in self.coworkers.values():
            if cw.config.tenant_id == tenant_id and cw.config.folder == folder:
                return cw
        return None

    def get_conversation(self, conversation_id: str) -> tuple[CoworkerState, ConversationState] | None:
        """Find a conversation state across all coworkers."""
        for cw in self.coworkers.values():
            for conv in cw.conversations.values():
                if conv.conversation.id == conversation_id:
                    return cw, conv
        return None

    def find_conversation_by_binding_and_chat(
        self, binding_id: str, channel_chat_id: str
    ) -> tuple[CoworkerState, ConversationState] | None:
        """Find conversation by channel binding ID and chat ID."""
        for cw in self.coworkers.values():
            binding = None
            for b in cw.channel_bindings.values():
                if b.id == binding_id:
                    binding = b
                    break
            if binding is None:
                continue
            for conv in cw.conversations.values():
                if (
                    conv.conversation.channel_binding_id == binding_id
                    and conv.conversation.channel_chat_id == channel_chat_id
                ):
                    return cw, conv
        return None
