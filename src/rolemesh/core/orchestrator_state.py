"""Centralized runtime state for the orchestrator.

Replaces module-level globals (_sessions, _registered_groups, _queue, etc.)
with structured, multi-tenant-aware state objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.core.types import (
        ChannelBinding,
        Conversation,
        Coworker,
        McpServerConfig,
        Skill,
        Tenant,
    )


@dataclass
class ConversationState:
    """Per-conversation runtime state."""

    conversation: Conversation
    session_id: str | None = None
    last_agent_timestamp: str = ""


@dataclass
class CoworkerState:
    """Per-coworker runtime state.

    ``config`` is the DB-row shape (``Coworker``) used as the single source of
    truth for all coworker fields — keeping the DB shape and the runtime
    cache cleanly separated.

    ``mcp_configs`` is the projected list of MCP server bindings (the
    junction-joined view returned by
    :func:`rolemesh.db.list_coworker_mcp_configs`). It is kept on the
    state object so request-path consumers (container_executor,
    mcp_publisher) read the cache instead of hitting Postgres on every
    spawn. The orchestrator boot loop populates it once; the
    ``web.coworker.restart`` / ``web.coworker.mcp_changed`` subscribers
    re-fetch and overwrite it.

    ``skills`` mirrors ``mcp_configs`` for the v1.1 03b per-tenant
    catalog. Populated via the same boot loop (calling
    :func:`rolemesh.db.list_skills_for_coworker` with
    ``enabled_only=True``) and refreshed by the
    ``web.coworker.skills_changed`` subscriber.
    """

    config: Coworker
    conversations: dict[str, ConversationState] = field(default_factory=dict)
    channel_bindings: dict[str, ChannelBinding] = field(default_factory=dict)
    mcp_configs: list[McpServerConfig] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)

    @staticmethod
    def from_coworker(
        cw: Coworker,
        mcp_configs: list[McpServerConfig] | None = None,
        skills: list[Skill] | None = None,
    ) -> CoworkerState:
        """Build a fresh ``CoworkerState`` from a ``Coworker`` DB row."""
        return CoworkerState(
            config=cw,
            mcp_configs=list(mcp_configs) if mcp_configs else [],
            skills=list(skills) if skills else [],
        )


class OrchestratorState:
    """All runtime state, structured by tenant and coworker.

    Concurrency is tracked along two orthogonal axes (slot-follows-turn rework):

    * **Turn admission** (``*_active`` counters, three levels). A *turn* is an
      in-flight agent invocation — it holds a slot from the moment processing
      starts until the batch-final (``is_final``) result. A warm idle container
      between turns holds **no** turn slot. This is the fairness / concurrent-work
      ceiling, enforced per global / tenant / coworker by
      :meth:`can_start_container` (kept under its historical name for call-site
      stability — read it as "can start a turn").

    * **Live containers** (``live_containers``, global only). Total containers
      alive (processing **or** warm). This is the memory ceiling; warm
      containers count here but not against turn admission. A new spawn checks
      :meth:`can_spawn_container`; the scheduler evicts an LRU warm container
      when this is the binding constraint.
    """

    def __init__(self, global_limit: int = 20) -> None:
        self.tenants: dict[str, Tenant] = {}
        self.coworkers: dict[str, CoworkerState] = {}

        # Turn-admission counters (three levels). Incremented when a turn starts
        # processing, decremented at is_final — NOT for the warm idle window.
        self.global_active: int = 0
        self.global_limit: int = global_limit
        self.tenant_active: dict[str, int] = {}
        self.coworker_active: dict[str, int] = {}

        # Live-container counter (memory ceiling). Incremented at spawn,
        # decremented at container exit — spans the warm window. Bounded by the
        # same ``global_limit`` (a container always backs at most one turn, so
        # live_containers >= global_active always holds).
        self.live_containers: int = 0

    def can_start_container(self, tenant_id: str, coworker_id: str) -> bool:
        """Check three-level **turn** admission (concurrent in-flight turns).

        Named ``can_start_container`` for historical call-site stability; since
        the slot-follows-turn rework it gates concurrent *turns*, not container
        lifetime. Warm idle containers do not count here.
        """
        if self.global_active >= self.global_limit:
            return False
        tenant = self.tenants.get(tenant_id)
        if tenant and self.tenant_active.get(tenant_id, 0) >= tenant.max_concurrent_containers:
            return False
        cw = self.coworkers.get(coworker_id)
        return not (cw and self.coworker_active.get(coworker_id, 0) >= cw.config.max_concurrent_containers)

    def increment_active(self, tenant_id: str, coworker_id: str) -> None:
        """Acquire a turn slot at all three levels (turn started processing)."""
        self.global_active += 1
        self.tenant_active[tenant_id] = self.tenant_active.get(tenant_id, 0) + 1
        self.coworker_active[coworker_id] = self.coworker_active.get(coworker_id, 0) + 1

    def decrement_active(self, tenant_id: str, coworker_id: str) -> None:
        """Release a turn slot at all three levels (turn reached is_final)."""
        self.global_active = max(0, self.global_active - 1)
        self.tenant_active[tenant_id] = max(0, self.tenant_active.get(tenant_id, 0) - 1)
        self.coworker_active[coworker_id] = max(0, self.coworker_active.get(coworker_id, 0) - 1)

    def can_spawn_container(self) -> bool:
        """Whether a new container fits under the global live-container ceiling.

        Distinct from :meth:`can_start_container`: a warm-container *resume*
        needs only turn admission (no new container), while a cold start needs
        both turn admission and a free live-container slot here.
        """
        return self.live_containers < self.global_limit

    def acquire_container(self) -> None:
        """Account a newly spawned (or adopted) live container."""
        self.live_containers += 1

    def release_container(self) -> None:
        """Account a container that has exited (or been reaped)."""
        self.live_containers = max(0, self.live_containers - 1)

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
