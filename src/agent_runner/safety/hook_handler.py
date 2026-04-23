"""HookHandler that runs the Safety pipeline on agent hook events.

Registered in ``agent_runner.main`` only when ``init.safety_rules`` is
non-empty — zero rules means zero runtime cost, mirroring the
ApprovalHookHandler convention.

Stage coverage (V2 P0.1):

  - on_user_prompt_submit  → Stage.INPUT_PROMPT      (control)
  - on_pre_tool_use        → Stage.PRE_TOOL_CALL     (control, V1)
  - on_post_tool_use       → Stage.POST_TOOL_RESULT  (observational)
  - on_pre_compact         → Stage.PRE_COMPACTION    (observational)

MODEL_OUTPUT is handled on the orchestrator side (see
``rolemesh.safety.engine.SafetyEngine.run_orchestrator_pipeline``) —
not in the container — so we don't bounce server-produced text through
another round-trip.

V1 only translated ``block``. P0.1 keeps that constraint: the pipeline
still rejects redact / warn / require_approval, so this handler only
needs to map block into each backend verdict. P0.2 extends to warn /
redact / require_approval once the pipeline allows those actions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rolemesh.safety.types import SafetyContext, Stage, ToolInfo

from .pipeline import pipeline_run

if TYPE_CHECKING:
    from agent_runner.hooks.events import (
        CompactionEvent,
        ToolCallEvent,
        ToolCallVerdict,
        ToolResultEvent,
        ToolResultVerdict,
        UserPromptEvent,
        UserPromptVerdict,
    )
    from agent_runner.tools.context import ToolContext

    from .registry import CheckRegistry


_log = logging.getLogger(__name__)


class SafetyHookHandler:
    """Unified hook that evaluates safety rules across every stage
    the container is responsible for.
    """

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

    # -- Context construction helper -----------------------------------

    def _build_context(
        self,
        *,
        stage: Stage,
        payload: dict[str, Any],
        tool: ToolInfo | None = None,
    ) -> SafetyContext:
        return SafetyContext(
            stage=stage,
            tenant_id=self._tool_ctx.tenant_id,
            coworker_id=self._tool_ctx.coworker_id,
            user_id=self._tool_ctx.user_id,
            job_id=self._tool_ctx.job_id,
            conversation_id=self._tool_ctx.conversation_id,
            payload=payload,
            tool=tool,
        )

    # -- PRE_TOOL_CALL (V1) --------------------------------------------

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        # Lazy imports so this module stays importable when the hooks
        # package is stubbed out in tests.
        from agent_runner.hooks.events import ToolCallVerdict

        ctx = self._build_context(
            stage=Stage.PRE_TOOL_CALL,
            payload={
                "tool_name": event.tool_name,
                "tool_input": dict(event.tool_input),
            },
            tool=ToolInfo(
                name=event.tool_name,
                reversible=self._tool_ctx.get_tool_reversibility(
                    event.tool_name
                ),
            ),
        )

        verdict = await pipeline_run(
            self._rules,
            self._registry,
            ctx,
            publisher=self._tool_ctx.publish,
        )

        if verdict.action == "block":
            return ToolCallVerdict(
                block=True,
                reason=verdict.reason or "Blocked by safety policy",
            )
        # allow is the only other V1 outcome — pipeline rejects
        # redact/warn/require_approval so we never land here with a
        # verdict the hook bridge cannot translate.
        return None

    # -- INPUT_PROMPT (P0.1) -------------------------------------------

    async def on_user_prompt_submit(
        self, event: UserPromptEvent
    ) -> UserPromptVerdict | None:
        from agent_runner.hooks.events import UserPromptVerdict

        ctx = self._build_context(
            stage=Stage.INPUT_PROMPT,
            payload={"prompt": event.prompt},
        )
        verdict = await pipeline_run(
            self._rules,
            self._registry,
            ctx,
            publisher=self._tool_ctx.publish,
        )
        if verdict.action == "block":
            return UserPromptVerdict(
                block=True,
                reason=verdict.reason or "Blocked by safety policy",
            )
        return None

    # -- POST_TOOL_RESULT (P0.1) ---------------------------------------

    async def on_post_tool_use(
        self, event: ToolResultEvent
    ) -> ToolResultVerdict | None:
        from agent_runner.hooks.events import ToolResultVerdict

        ctx = self._build_context(
            stage=Stage.POST_TOOL_RESULT,
            payload={
                "tool_name": event.tool_name,
                "tool_input": dict(event.tool_input),
                "tool_result": event.tool_result,
                "is_error": event.is_error,
            },
            tool=ToolInfo(
                name=event.tool_name,
                reversible=self._tool_ctx.get_tool_reversibility(
                    event.tool_name
                ),
            ),
        )
        verdict = await pipeline_run(
            self._rules,
            self._registry,
            ctx,
            publisher=self._tool_ctx.publish,
        )
        # POST_TOOL_RESULT is observational for the pipeline, but the
        # hook contract only exposes ``appended_context`` (see
        # agent_runner/hooks/events.py docstring — replace-result is
        # intentionally unsupported). A ``block`` verdict here means
        # "withhold the result from the agent" — the side effect has
        # already happened, so we turn it into a context annotation so
        # the agent is informed rather than silently misled.
        if verdict.action == "block":
            return ToolResultVerdict(
                appended_context=(
                    "[Tool result withheld by safety policy: "
                    f"{verdict.reason or 'policy match'}]"
                )
            )
        return None

    # -- PRE_COMPACTION (P0.1) -----------------------------------------

    async def on_pre_compact(self, event: CompactionEvent) -> None:
        ctx = self._build_context(
            stage=Stage.PRE_COMPACTION,
            payload={
                "transcript_path": event.transcript_path,
                "messages": list(event.messages),
            },
        )
        # Observational: HookRegistry.emit_pre_compact already
        # try/excepts, so any pipeline failure is contained without
        # affecting peer handlers or the agent turn. We deliberately
        # do NOT add a second try/except here — it would mask genuine
        # pipeline bugs that the registry-level handler is designed
        # to surface via warning logs.
        await pipeline_run(
            self._rules,
            self._registry,
            ctx,
            publisher=self._tool_ctx.publish,
        )


__all__ = ["SafetyHookHandler"]
