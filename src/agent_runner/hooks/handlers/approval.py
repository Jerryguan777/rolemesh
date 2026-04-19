"""PreToolUse handler that blocks external MCP calls needing approval.

Registered in main.py ONLY when init.approval_policies is non-empty.
If no policies apply, this handler is never registered and has exactly
zero runtime cost — the approval module is invisible to agents that do
not have gated tools.

Scope:
  - External MCP tools only: tool names matching ``mcp__<server>__<tool>``.
  - Built-in RoleMesh tools (``mcp__rolemesh__*``) are NEVER gated — they
    are the only way the agent can ask for approval (submit_proposal) or
    communicate progress to the user, and blocking them would deadlock
    the feature.
  - Non-MCP tools (Read, Bash, etc.) are out of scope for policy-based
    approval; approval is about external side effects.

Behaviour on match:
  1. Publish an ``auto_approval_request`` NATS task to the orchestrator.
  2. Return a block verdict telling the agent WHY the call was blocked
     and suggesting submit_proposal for follow-up batching.

The hook chain is fail-close at the control-hook level (see
HookRegistry docstring): if this handler raises, the backend bridge
translates that into a block, which is the safe default for an approval
gate.

Design note: we deliberately do NOT track an ``_aborted`` flag per
turn. The backend's ``_aborting`` state already guards against
mid-abort tool invocation (see ``docs/backend-stop-contract.md`` items
3-5), and adding a second flag here risks a handler-singleton staleness
bug that survives turn boundaries.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent_runner.approval.policy import compute_action_hash, find_matching_policy

if TYPE_CHECKING:
    from agent_runner.tools.context import ToolContext

    from ..events import ToolCallEvent, ToolCallVerdict

_log = logging.getLogger(__name__)

_EXTERNAL_MCP_PREFIX = "mcp__"
_ROLEMESH_BUILTIN_PREFIX = "mcp__rolemesh__"


class ApprovalHookHandler:
    """PreToolUse hook that gates external MCP calls via policy match."""

    def __init__(
        self,
        policies: list[dict[str, Any]],
        tool_ctx: ToolContext,
    ) -> None:
        # Store a reference to the list — we don't copy it because the
        # approval policies snapshot is fixed per AgentInitData, and any
        # mutation by the orchestrator would only land on the next run.
        self._policies = policies
        self._tool_ctx = tool_ctx

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        # Lazy-import the verdict type so this module stays importable
        # even when the hooks package is stubbed out in tests.
        from ..events import ToolCallVerdict

        server, tool = _parse_mcp_tool_name(event.tool_name)
        if server is None or tool is None:
            return None

        # Only the external MCP surface is subject to approval policies.
        # Built-in rolemesh tools (send_message, submit_proposal, ...) are
        # always allowed — see module docstring.
        if event.tool_name.startswith(_ROLEMESH_BUILTIN_PREFIX):
            return None

        policy = find_matching_policy(self._policies, server, tool, event.tool_input)
        if policy is None:
            return None

        action_hash = compute_action_hash(tool, event.tool_input)

        # Fire-and-forget publish. If the NATS publish itself raises, we
        # still want to BLOCK (fail-closed). The backend bridge converts
        # the exception into a block verdict, so we do not need to catch
        # it here — letting it propagate is the right behaviour.
        self._tool_ctx.publish(
            f"agent.{self._tool_ctx.job_id}.tasks",
            {
                "type": "auto_approval_request",
                "mcp_server_name": server,
                "tool_name": tool,
                "tool_params": event.tool_input,
                "action_hash": action_hash,
                "policy_id": str(policy.get("id", "")),
                "tenantId": self._tool_ctx.tenant_id,
                "coworkerId": self._tool_ctx.coworker_id,
                "conversationId": self._tool_ctx.conversation_id,
                "groupFolder": self._tool_ctx.group_folder,
                "jobId": self._tool_ctx.job_id,
                "userId": self._tool_ctx.user_id,
            },
        )

        policy_id_str = str(policy.get("id", "unknown"))
        reason = (
            f"This operation requires human approval "
            f"(policy: {policy_id_str[:8]}). "
            "An approval request has been submitted automatically. "
            "You can also use submit_proposal to bundle multiple related "
            "actions with a rationale."
        )
        return ToolCallVerdict(block=True, reason=reason)


def _parse_mcp_tool_name(tool_name: str) -> tuple[str | None, str | None]:
    """Parse ``mcp__<server>__<tool>`` into (server, tool).

    Returns (None, None) for anything that is not an MCP tool — includes
    plain tool names (Bash, Read) and malformed mcp names.

    Server and tool names themselves may contain underscores, but the
    separator is always ``__`` (two underscores); ``split("__", 2)``
    tolerates that.
    """
    if not tool_name.startswith(_EXTERNAL_MCP_PREFIX):
        return (None, None)
    parts = tool_name.split("__", 2)
    if len(parts) < 3 or not parts[1] or not parts[2]:
        return (None, None)
    return (parts[1], parts[2])
