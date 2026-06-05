"""HookHandler that runs the Safety pipeline on agent hook events.

Registered in ``agent_runner.main`` only when ``init.safety_rules`` is
non-empty — zero rules means zero runtime cost.

Stage coverage:

  - on_user_prompt_submit  → Stage.INPUT_PROMPT      (control)
  - on_pre_tool_use        → Stage.PRE_TOOL_CALL     (control)
  - on_post_tool_use       → Stage.POST_TOOL_RESULT  (observational)
  - on_pre_compact         → Stage.PRE_COMPACTION    (observational)

MODEL_OUTPUT is handled on the orchestrator side (see
``rolemesh.safety.engine.SafetyEngine.run_orchestrator_pipeline``) —
not in the container — so we don't bounce server-produced text through
another round-trip.

Verdict translation (V2 P0.2):

  - block              → backend block verdict (UserPromptVerdict.block
                         or ToolCallVerdict.block). On POST_TOOL_RESULT,
                         where the hook protocol deliberately does not
                         expose a replace-result channel, the handler
                         emits ``appended_context`` with a withhold
                         notice instead.
  - require_approval   → at PRE_TOOL_CALL, when an ``ApprovalAwaiter`` is
                         wired, bridged into a HITL approval ticket
                         (docs/21 §11.4): the call blocks until a human
                         decides — approve runs the tool in-band, reject /
                         timeout blocks. Everywhere else (no approval
                         surface) it stays a ``block`` alias. The audit
                         still records ``verdict_action=require_approval``.
  - redact             → PRE_TOOL_CALL replaces ``tool_input``.
                         POST_TOOL_RESULT falls back to appended_context
                         because the hook protocol cannot replace the
                         tool result (see hooks/events.py module
                         docstring).
                         INPUT_PROMPT downgrades to block — the SDK
                         hook has no payload mutation surface there.
  - warn               → ``appended_context`` on the backends that
                         carry one (INPUT_PROMPT, POST_TOOL_RESULT).
                         PRE_TOOL_CALL has no context channel so warn
                         is a pure audit event.
  - allow              → None (no handler verdict).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rolemesh.safety.types import SafetyContext, Stage, ToolInfo

from .pipeline import pipeline_run

if TYPE_CHECKING:
    from agent_runner.approval.awaiter import ApprovalAwaiter
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
    from rolemesh.safety.types import Verdict

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
        approval_awaiter: ApprovalAwaiter | None = None,
    ) -> None:
        # Rules are a snapshot taken at container start. Hot-update
        # semantics (§5.1) applies at the container restart boundary;
        # we intentionally keep this reference rather than copying.
        self._rules = rules
        self._registry = registry
        self._tool_ctx = tool_ctx
        # When wired (HITL is active for the run), a PRE_TOOL_CALL
        # require_approval verdict is bridged into a real HITL approval ticket
        # instead of a terminal block. None preserves the legacy behaviour
        # (require_approval == block) for any stage/run without an approval
        # surface. Only PRE_TOOL_CALL uses it — see docs/21 §11.4.
        self._approval_awaiter = approval_awaiter

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

    # -- PRE_TOOL_CALL --------------------------------------------------

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

        if (
            verdict.action == "require_approval"
            and self._approval_awaiter is not None
        ):
            # Safety->approval bridge (docs/21 §11.4): PRE_TOOL_CALL is the one
            # stage with both an awaiting agent and an approval surface, so a
            # require_approval verdict becomes a HITL ticket. approve -> the
            # tool runs in-band (return None); reject/timeout -> block.
            return await self._request_approval(event, verdict)

        if verdict.action in ("block", "require_approval"):
            # block — or require_approval with no approval surface wired —
            # refuses the turn identically.
            reason = verdict.reason or "Blocked by safety policy"
            return ToolCallVerdict(block=True, reason=reason)

        if verdict.action == "redact":
            # Redact chain produced a modified payload; swap the
            # tool_input. The tool_name is taken from the original
            # event; if a check tried to rewrite tool_name that's out
            # of scope for this stage and silently ignored here.
            modified = verdict.modified_payload or {}
            new_input = modified.get("tool_input") if isinstance(modified, dict) else None
            if isinstance(new_input, dict):
                return ToolCallVerdict(
                    block=False, modified_input=new_input
                )
            _log.error(
                "safety: redact on pre_tool_use but modified_payload has "
                "no tool_input key — ignoring"
            )
            return None

        # warn verdicts on PRE_TOOL_CALL carry no channel (ToolCallVerdict
        # has no appended_context). The audit event has already been
        # published; nothing more for the agent here.
        return None

    async def _request_approval(
        self, event: ToolCallEvent, verdict: Verdict
    ) -> ToolCallVerdict | None:
        """Bridge a PRE_TOOL_CALL require_approval verdict into a HITL ticket.

        Builds the approval request body (with the safety ``triggered_by``
        provenance) and blocks on the shared awaiter, exactly like a
        business-policy approval. ``approve`` lets the tool run in-band;
        ``reject`` / timeout returns a block verdict with a safety-flavoured
        reason. Only reachable when ``self._approval_awaiter`` is wired.
        """
        from agent_runner.hooks.events import ToolCallVerdict
        from agent_runner.hooks.handlers.approval import parse_mcp_tool_name

        assert self._approval_awaiter is not None
        tool_name = event.tool_name or ""
        parsed = parse_mcp_tool_name(tool_name)
        # MCP tools split into (server, tool); a builtin tool (Bash/Edit/...)
        # has no server, so carry the bare name as tool_name and leave
        # mcp_server_name empty. The SPA keys the safety banner off
        # triggered_by, not the server name, so an empty server is fine.
        if parsed is not None:
            server, tool = parsed
        else:
            server, tool = "", tool_name
        params = dict(event.tool_input) if isinstance(event.tool_input, dict) else {}

        triggered_by = {
            "kind": "safety_rule",
            "rule_id": verdict.firing_rule_id or "",
            "check_id": verdict.firing_check_id or "",
            "stage": Stage.PRE_TOOL_CALL.value,
        }
        decision = await self._approval_awaiter.await_decision(
            {
                "tenant_id": self._tool_ctx.tenant_id,
                "coworker_id": self._tool_ctx.coworker_id,
                "conversation_id": self._tool_ctx.conversation_id or None,
                # Approver = the user whose turn this is; a null id is forwarded
                # as-is so the orchestrator fails closed on it (§3.1).
                "user_id": self._tool_ctx.user_id or None,
                # Not a business policy — provenance travels in triggered_by.
                "policy_id": None,
                "mcp_server_name": server,
                "tool_name": tool,
                "params": params,
                "action_summary": f"{tool} held for approval by a safety rule",
                "rationale": verdict.reason,
                "triggered_by": triggered_by,
            }
        )
        if decision.approved:
            return None
        if decision.timed_out:
            return ToolCallVerdict(
                block=True,
                reason=(
                    f"Safety approval for {tool} timed out without a decision; "
                    "the tool call was not executed."
                ),
            )
        reason = verdict.reason or "Tool call blocked by a safety rule."
        if decision.note:
            reason = f"{reason} Note: {decision.note}"
        return ToolCallVerdict(block=True, reason=reason)

    # -- INPUT_PROMPT ---------------------------------------------------

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
        if verdict.action in ("block", "require_approval"):
            reason = verdict.reason or "Blocked by safety policy"
            return UserPromptVerdict(block=True, reason=reason)

        if verdict.action == "redact":
            # Claude SDK's UserPromptSubmit hook exposes block or
            # appended_context — there is no prompt-replacement channel.
            # Downgrade to block + surface a SAFETY.REDACT_UNSUPPORTED_ON_STAGE
            # warning so operators notice their rule had no real effect
            # (they should move the rule to MODEL_OUTPUT or switch to a
            # block-style check on INPUT_PROMPT).
            _log.warning(
                "safety: SAFETY.REDACT_UNSUPPORTED_ON_STAGE — redact "
                "requested on INPUT_PROMPT; downgrading to block",
                extra={
                    "tenant_id": self._tool_ctx.tenant_id,
                    "coworker_id": self._tool_ctx.coworker_id,
                },
            )
            return UserPromptVerdict(
                block=True,
                reason=(
                    verdict.reason
                    or "Blocked: prompt matched redact rule on a stage "
                    "that does not support redaction"
                ),
            )

        if verdict.action == "warn" and verdict.appended_context:
            return UserPromptVerdict(
                appended_context=verdict.appended_context
            )
        return None

    # -- POST_TOOL_RESULT -----------------------------------------------

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
        # POST_TOOL_RESULT hook only exposes appended_context. Block,
        # require_approval, and redact all funnel through that same
        # channel. require_approval is treated identically to block.
        if verdict.action in ("block", "require_approval"):
            return ToolResultVerdict(
                appended_context=(
                    f"[Tool result withheld by safety policy: "
                    f"{verdict.reason or 'policy match'}]"
                )
            )
        if verdict.action == "redact":
            # Redact can't actually replace the tool result on this hook
            # (protocol limitation — see hooks/events.py docstring). Emit
            # the modified text as appended_context so the agent sees
            # the cleaned view; the original result still reaches the
            # transcript unredacted, but the agent is now aware.
            modified = verdict.modified_payload or {}
            cleaned = (
                modified.get("tool_result")
                if isinstance(modified, dict)
                else None
            )
            msg = (
                f"[Tool result redacted by safety policy: {cleaned}]"
                if isinstance(cleaned, str)
                else (
                    "[Tool result flagged for redaction by safety "
                    "policy but could not be replaced on this hook]"
                )
            )
            return ToolResultVerdict(appended_context=msg)
        if verdict.action == "warn" and verdict.appended_context:
            return ToolResultVerdict(
                appended_context=verdict.appended_context
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
