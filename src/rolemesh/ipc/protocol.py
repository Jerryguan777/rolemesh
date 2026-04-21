"""IPC message types for NATS-based communication between Orchestrator and Agent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from rolemesh.auth.permissions import AgentPermissions


@dataclass(frozen=True)
class McpServerSpec:
    """MCP server specification passed to the agent container.

    Unlike McpServerConfig, this contains the rewritten URL (pointing to
    the credential proxy) and no auth token. The proxy injects the token.
    """

    name: str  # registered name, e.g. "my-mcp-server"
    type: str  # "sse" or "http"
    url: str  # proxy URL, e.g. "http://host.docker.internal:3001/mcp-proxy/my-mcp-server/"


@dataclass(frozen=True)
class AgentInitData:
    """Channel 1: initial input written to KV before container starts.

    Shared between orchestrator (serialize) and agent runner (deserialize).
    """

    prompt: str
    group_folder: str
    chat_jid: str
    permissions: dict[str, object] = field(default_factory=dict)
    tenant_id: str = ""
    coworker_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    system_prompt: str | None = None
    role_config: dict[str, object] | None = None
    mcp_servers: list[McpServerSpec] | None = None
    # Approval policies applicable to this agent/user. None means "approval
    # module is inactive for this run" — the container MUST NOT register
    # ApprovalHookHandler in that case, so approvals are zero-impact when
    # nobody configured them.
    approval_policies: list[dict[str, object]] | None = None
    # Safety Framework rules snapshot. None means "safety is inactive for
    # this run" — the container MUST NOT register SafetyHookHandler in
    # that case. Shape is the dict form produced by
    # ``rolemesh.safety.types.Rule.to_snapshot_dict``.
    safety_rules: list[dict[str, object]] | None = None

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> AgentInitData:
        raw = json.loads(data)
        mcp_raw = raw.get("mcp_servers")
        mcp_servers = [McpServerSpec(**s) for s in mcp_raw] if mcp_raw else None

        # Backward compat: convert legacy is_main bool to permissions dict
        if "is_main" in raw and "permissions" not in raw:
            is_main = raw["is_main"]
            permissions = AgentPermissions.for_role(
                "super_agent" if is_main else "agent"
            ).to_dict()
        else:
            permissions = raw.get("permissions") or AgentPermissions().to_dict()

        return cls(
            prompt=raw["prompt"],
            group_folder=raw["group_folder"],
            chat_jid=raw["chat_jid"],
            permissions=permissions,
            tenant_id=raw.get("tenant_id", ""),
            coworker_id=raw.get("coworker_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            user_id=raw.get("user_id", ""),
            session_id=raw.get("session_id"),
            is_scheduled_task=raw.get("is_scheduled_task", False),
            assistant_name=raw.get("assistant_name"),
            system_prompt=raw.get("system_prompt"),
            role_config=raw.get("role_config"),
            mcp_servers=mcp_servers,
            approval_policies=raw.get("approval_policies"),
            safety_rules=raw.get("safety_rules"),
        )
