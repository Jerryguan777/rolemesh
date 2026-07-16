"""IPC message types for NATS-based communication between Orchestrator and Agent.

Deserialization routes payloads through ``from_dict_filter_unknown``
so the agent runner happily ignores fields a newer orchestrator
introduces — forward-compat across rolling upgrades (INV-2).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.ipc._unknown_filter import from_dict_filter_unknown


@dataclass(frozen=True)
class McpServerSpec:
    """MCP server specification passed to the agent container.

    Unlike McpServerConfig, this contains the rewritten URL (pointing to
    the credential proxy) and no auth token. The proxy injects the token.
    """

    name: str  # registered name, e.g. "my-mcp-server"
    type: str  # "sse" or "http"
    url: str  # proxy URL, e.g. "http://egress-gateway:3001/mcp-proxy/my-mcp-server/"
    # V2 P0.4: per-tool reversibility override. Forwarded from
    # ``McpServerConfig.tool_reversibility`` via the orchestrator so
    # the container's ToolContext can answer
    # ``get_tool_reversibility(tool_name)`` at hook time without a
    # DB round-trip.
    tool_reversibility: dict[str, bool] = field(default_factory=dict)


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
    # Run attribution (single-writer refactor): the ``runs`` row
    # this initial prompt answers. The container echoes it (and the ids of
    # queued follow-ups) back on every output event so the orchestrator can
    # terminal-write the RIGHT run without relying on a closure variable
    # that goes stale on warm-container follow-ups. None = older
    # orchestrator or a turn with no run (scheduled task).
    run_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    system_prompt: str | None = None
    role_config: dict[str, object] | None = None
    mcp_servers: list[McpServerSpec] | None = None
    # Safety Framework rules snapshot. None means "safety is inactive for
    # this run" — the container MUST NOT register SafetyHookHandler in
    # that case. Shape is the dict form produced by
    # ``rolemesh.safety.types.Rule.to_snapshot_dict``.
    safety_rules: list[dict[str, object]] | None = None
    # Metadata for slow safety checks the orchestrator hosts. The
    # container registers a RemoteCheck proxy per spec so the pipeline
    # can reference slow-check ids by name. None means "no slow checks
    # available" — the container will skip any rule pointing at an
    # id it doesn't have locally and log a warning (existing unknown-
    # check behaviour). Each spec carries {check_id, version, stages,
    # cost_class, supported_codes, default_timeout_ms}.
    slow_check_specs: list[dict[str, object]] | None = None
    # HITL approval policy snapshot (docs/12-hitl-approval-architecture.md §4). Each
    # item is the dict form of an ``ApprovalPolicy``: {id, tenant_id,
    # mcp_server_name, tool_name, condition_expr, enabled, priority,
    # updated_at(iso8601)}. None / empty means "no approval gating this run" —
    # the container does not register the approval hook (mirrors safety_rules).
    # The orchestrator builds this from ``approval_policies`` rows; the
    # container matches against it locally so a blocked MCP tool call needs no
    # DB round-trip.
    approval_policies: list[dict[str, object]] | None = None

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> AgentInitData:
        raw = json.loads(data)

        # Nested dataclass: route each McpServerSpec through the same
        # filter so a future orchestrator adding fields cannot break an
        # older container.
        mcp_raw = raw.get("mcp_servers")
        if mcp_raw is not None:
            raw["mcp_servers"] = [
                from_dict_filter_unknown(McpServerSpec, s) for s in mcp_raw
            ]

        # Falsy permissions (missing/None/empty dict) → least-privilege
        # defaults, matching the pre-refactor ``raw.get(...) or ...``
        # semantics.
        if not raw.get("permissions"):
            raw["permissions"] = AgentPermissions().to_dict()

        return from_dict_filter_unknown(cls, raw)
