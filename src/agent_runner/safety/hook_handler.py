"""HookHandler that runs the Safety pipeline on PRE_TOOL_CALL events.

Registered in ``agent_runner.main`` only when ``init.safety_rules`` is
non-empty — zero rules means zero runtime cost, mirroring the
ApprovalHookHandler convention.

V1 only implements ``on_pre_tool_use``. V2 will extend with
``on_user_prompt_submit`` / ``on_post_tool_use`` / ``on_pre_compact``;
the class already takes the pipeline / registry / tool_ctx triple so
adding methods is purely additive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rolemesh.safety.types import SafetyContext, Stage, ToolInfo

from .pipeline import pipeline_run

if TYPE_CHECKING:
    from agent_runner.hooks.events import ToolCallEvent, ToolCallVerdict
    from agent_runner.tools.context import ToolContext

    from .registry import CheckRegistry


_log = logging.getLogger(__name__)


class SafetyHookHandler:
    """PreToolUse hook that evaluates safety rules against tool calls."""

    def __init__(
        self,
        *,
        rules: list[dict[str, Any]],
        registry: CheckRegistry,
        tool_ctx: ToolContext,
    ) -> None:
        # Rules are a snapshot taken at container start. Hot-update
        # semantics (§5.1) applies at the container restart boundary;
        # we intentionally keep this reference rather than copying.
        self._rules = rules
        self._registry = registry
        self._tool_ctx = tool_ctx

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        # Lazy import so this module stays importable when the hooks
        # package is stubbed out in tests.
        from agent_runner.hooks.events import ToolCallVerdict

        ctx = SafetyContext(
            stage=Stage.PRE_TOOL_CALL,
            tenant_id=self._tool_ctx.tenant_id,
            coworker_id=self._tool_ctx.coworker_id,
            user_id=self._tool_ctx.user_id,
            job_id=self._tool_ctx.job_id,
            conversation_id=self._tool_ctx.conversation_id,
            payload={
                "tool_name": event.tool_name,
                "tool_input": dict(event.tool_input),
            },
            tool=ToolInfo(name=event.tool_name, reversible=False),
        )

        verdict = await pipeline_run(
            self._rules,
            self._registry,
            ctx,
            publisher=self._tool_ctx.publish,
        )

        if verdict.action == "block":
            return ToolCallVerdict(
                block=True, reason=verdict.reason or "Blocked by safety policy"
            )
        if verdict.action == "redact" and isinstance(verdict.modified_payload, dict):
            modified = verdict.modified_payload.get("tool_input")
            if isinstance(modified, dict):
                return ToolCallVerdict(block=False, modified_input=modified)
        # allow / warn / require_approval (V2) all fall through to no-op
        # at V1 — warn would inject context via a V2-specific field.
        return None


__all__ = ["SafetyHookHandler"]
