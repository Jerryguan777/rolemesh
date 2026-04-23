"""Tests for SafetyHookHandler.

Focus: the adapter layer between agent_runner hook events and the
pipeline. Uses a minimal fake ToolContext so tests stay DB/NATS-free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from agent_runner.hooks.events import (
    CompactionEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserPromptEvent,
)
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_container_registry
from rolemesh.safety.types import Stage

from .conftest import make_rule

if TYPE_CHECKING:
    from rolemesh.safety.registry import CheckRegistry


@dataclass
class _FakeToolCtx:
    tenant_id: str = "tenant-1"
    coworker_id: str = "cw-1"
    user_id: str = "user-1"
    job_id: str = "job-1"
    conversation_id: str = "conv-1"
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Per-tool reversibility override table; defaults to "irreversible"
    # (P0.1 stub semantics) when no entry is present.
    reversibility: dict[str, bool] = field(default_factory=dict)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))

    def get_tool_reversibility(self, tool_name: str) -> bool:
        return self.reversibility.get(tool_name, False)


class TestPreToolUse:
    @pytest.mark.asyncio
    async def test_ssn_in_tool_input_returns_block_verdict(self) -> None:
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            rule_id="r1",
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolCallEvent(
            tool_name="github__create_issue",
            tool_input={"body": "leaked 123-45-6789"},
        )
        verdict = await handler.on_pre_tool_use(event)
        assert verdict is not None
        assert verdict.block is True
        assert verdict.reason and "PII.SSN" in verdict.reason

    @pytest.mark.asyncio
    async def test_clean_payload_returns_none(self) -> None:
        tool_ctx = _FakeToolCtx()
        rule = make_rule(config={"patterns": {"SSN": True}})
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolCallEvent(
            tool_name="github__create_issue",
            tool_input={"body": "hello world"},
        )
        verdict = await handler.on_pre_tool_use(event)
        assert verdict is None

    @pytest.mark.asyncio
    async def test_no_applicable_rules_returns_none(self) -> None:
        # Handler is registered but the single rule targets a different
        # stage — the path must gracefully return None rather than
        # returning an empty-audit block.
        tool_ctx = _FakeToolCtx()
        from rolemesh.safety.types import Stage

        rule = make_rule(stage=Stage.INPUT_PROMPT)
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolCallEvent(
            tool_name="github__create_issue",
            tool_input={"body": "leaked 123-45-6789"},
        )
        verdict = await handler.on_pre_tool_use(event)
        assert verdict is None

    @pytest.mark.asyncio
    async def test_block_publishes_audit_event(self) -> None:
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            rule_id="r-audit",
            config={"patterns": {"EMAIL": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolCallEvent(
            tool_name="slack__post_message",
            tool_input={"text": "ping bob@example.com"},
        )
        verdict = await handler.on_pre_tool_use(event)
        assert verdict is not None and verdict.block
        # Allow the test loop to drain any pending publishes synchronously.
        await asyncio.sleep(0)
        assert len(tool_ctx.events) == 1
        subject, data = tool_ctx.events[0]
        assert subject == "agent.job-1.safety_events"
        assert data["verdict_action"] == "block"
        assert data["triggered_rule_ids"] == ["r-audit"]
        assert data["stage"] == "pre_tool_call"

    @pytest.mark.asyncio
    async def test_passes_tenant_and_coworker_into_context(self) -> None:
        tool_ctx = _FakeToolCtx(
            tenant_id="tenant-X", coworker_id="cw-X", job_id="job-X"
        )
        rule = make_rule(config={"patterns": {"SSN": True}})
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolCallEvent(
            tool_name="x__y", tool_input={"z": "123-45-6789"}
        )
        await handler.on_pre_tool_use(event)
        assert tool_ctx.events
        _, data = tool_ctx.events[0]
        assert data["tenant_id"] == "tenant-X"
        assert data["coworker_id"] == "cw-X"
        assert data["job_id"] == "job-X"


# ---------------------------------------------------------------------------
# P0.1 additions — INPUT_PROMPT / POST_TOOL_RESULT / PRE_COMPACTION
# ---------------------------------------------------------------------------


class TestUserPromptSubmit:
    """INPUT_PROMPT is a control stage — pipeline exceptions must propagate
    and block verdicts must translate into UserPromptVerdict(block=True).
    """

    @pytest.mark.asyncio
    async def test_ssn_in_prompt_returns_block(self) -> None:
        tool_ctx = _FakeToolCtx()
        # PII regex check advertises INPUT_PROMPT in its stages set, so
        # a rule scoped to that stage must fire.
        rule = make_rule(
            stage=Stage.INPUT_PROMPT,
            check_id="pii.regex",
            config={"patterns": {"SSN": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        verdict = await handler.on_user_prompt_submit(
            UserPromptEvent(prompt="my ssn is 123-45-6789")
        )
        assert verdict is not None
        assert verdict.block is True
        assert verdict.reason and "PII.SSN" in verdict.reason

    @pytest.mark.asyncio
    async def test_clean_prompt_returns_none(self) -> None:
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            stage=Stage.INPUT_PROMPT,
            config={"patterns": {"EMAIL": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        verdict = await handler.on_user_prompt_submit(
            UserPromptEvent(prompt="hello, what can you do?")
        )
        assert verdict is None

    @pytest.mark.asyncio
    async def test_publishes_audit_for_block(self) -> None:
        tool_ctx = _FakeToolCtx(job_id="job-Z")
        rule = make_rule(
            rule_id="r-inp",
            stage=Stage.INPUT_PROMPT,
            config={"patterns": {"EMAIL": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        await handler.on_user_prompt_submit(
            UserPromptEvent(prompt="reach me at a@b.com")
        )
        await asyncio.sleep(0)
        assert tool_ctx.events
        subject, event = tool_ctx.events[0]
        assert subject == "agent.job-Z.safety_events"
        assert event["stage"] == "input_prompt"
        assert event["verdict_action"] == "block"
        assert event["triggered_rule_ids"] == ["r-inp"]

    @pytest.mark.asyncio
    async def test_pretool_rules_ignored_on_input_stage(self) -> None:
        # A rule scoped to PRE_TOOL_CALL must not fire on INPUT_PROMPT.
        # Otherwise the wrong payload shape (no prompt key) would be
        # fed to the check.
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            stage=Stage.PRE_TOOL_CALL, config={"patterns": {"SSN": True}}
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        verdict = await handler.on_user_prompt_submit(
            UserPromptEvent(prompt="my ssn is 123-45-6789")
        )
        # Rule scoped to PRE_TOOL_CALL must NOT fire on INPUT_PROMPT.
        assert verdict is None
        assert tool_ctx.events == []


class TestPostToolUse:
    """POST_TOOL_RESULT is observational. A block verdict here cannot
    reach the agent as "don't execute" (the call already happened),
    so the handler withholds the result via appended_context instead.
    """

    @pytest.mark.asyncio
    async def test_block_translates_to_withhold_message(self) -> None:
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            rule_id="r-post",
            stage=Stage.POST_TOOL_RESULT,
            config={"patterns": {"SSN": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolResultEvent(
            tool_name="github__search",
            tool_input={"query": "x"},
            tool_result="Found SSN 123-45-6789 in leak.txt",
        )
        verdict = await handler.on_post_tool_use(event)
        assert verdict is not None
        assert verdict.appended_context is not None
        assert "withheld" in verdict.appended_context
        assert "PII.SSN" in (verdict.appended_context or "")

    @pytest.mark.asyncio
    async def test_clean_result_returns_none(self) -> None:
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            stage=Stage.POST_TOOL_RESULT, config={"patterns": {"SSN": True}}
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolResultEvent(
            tool_name="github__search",
            tool_input={"query": "x"},
            tool_result="no matches",
        )
        verdict = await handler.on_post_tool_use(event)
        assert verdict is None

    @pytest.mark.asyncio
    async def test_passes_reversibility_from_tool_ctx(self) -> None:
        """Reversibility must come from the context helper, not be
        hardcoded at the hook. Allows P0.4 to swap in real values
        without re-touching the handler.
        """
        tool_ctx = _FakeToolCtx(
            reversibility={"github__create_pr": False, "Read": True}
        )
        # A stub check that captures the reversible flag it was handed.
        captured: list[bool] = []

        class _CaptureCheck:
            id = "stub.capture"
            version = "1"
            stages = frozenset({Stage.POST_TOOL_RESULT})
            cost_class = "cheap"
            supported_codes: frozenset[str] = frozenset()
            config_model = None

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                from rolemesh.safety.types import Verdict
                captured.append(ctx.tool.reversible if ctx.tool else None)
                return Verdict(action="allow")

        from rolemesh.safety.registry import CheckRegistry
        reg = CheckRegistry()
        reg.register(_CaptureCheck())
        rule = make_rule(
            check_id="stub.capture", stage=Stage.POST_TOOL_RESULT, config={}
        )
        handler = SafetyHookHandler(
            rules=[rule], registry=reg,
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        await handler.on_post_tool_use(
            ToolResultEvent(
                tool_name="github__create_pr",
                tool_input={},
                tool_result="ok",
            )
        )
        assert captured == [False]
        await handler.on_post_tool_use(
            ToolResultEvent(
                tool_name="Read", tool_input={}, tool_result="ok"
            )
        )
        assert captured == [False, True]

    @pytest.mark.asyncio
    async def test_observational_exception_swallowed_at_pipeline(self) -> None:
        # A buggy check on POST_TOOL_RESULT must not abort the turn.
        # Pipeline already fail-safes; the handler must not add a
        # second layer that hides the WARNING log.
        from rolemesh.safety.registry import CheckRegistry
        from rolemesh.safety.types import CostClass

        class _Raise:
            id = "stub.raise"
            version = "1"
            stages = frozenset({Stage.POST_TOOL_RESULT})
            cost_class: CostClass = "cheap"
            supported_codes: frozenset[str] = frozenset()
            config_model = None

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

        reg = CheckRegistry()
        reg.register(_Raise())
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            check_id="stub.raise", stage=Stage.POST_TOOL_RESULT, config={}
        )
        handler = SafetyHookHandler(
            rules=[rule], registry=reg,
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        # No exception — observational stage fail-safe.
        verdict = await handler.on_post_tool_use(
            ToolResultEvent(
                tool_name="x__y", tool_input={}, tool_result="hi"
            )
        )
        assert verdict is None


class TestPreCompact:
    """V1's only check (pii.regex) does not advertise PRE_COMPACTION,
    so these tests use a stub check that does. The point is to pin
    the handler <-> pipeline wiring for the stage — once a real
    PRE_COMPACTION check lands we still want this dispatch path to
    behave correctly.
    """

    def _registry_with_compaction_check(
        self, verdict_factory: Any
    ) -> CheckRegistry:
        from rolemesh.safety.registry import CheckRegistry

        class _Stub:
            id = "stub.compact"
            version = "1"
            stages = frozenset({Stage.PRE_COMPACTION})
            cost_class = "cheap"
            supported_codes: frozenset[str] = frozenset({"STUB.COMPACT"})
            config_model = None

            async def check(self, ctx, config):  # type: ignore[no-untyped-def]
                return verdict_factory(ctx)

        reg = CheckRegistry()
        reg.register(_Stub())
        return reg

    @pytest.mark.asyncio
    async def test_block_verdict_on_match_publishes_audit(self) -> None:
        from rolemesh.safety.types import Finding, Verdict

        def _block(_ctx: Any) -> Verdict:
            return Verdict(
                action="block",
                reason="compact match",
                findings=[
                    Finding(
                        code="STUB.COMPACT", severity="high", message="x"
                    )
                ],
            )

        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            rule_id="r-comp",
            check_id="stub.compact",
            stage=Stage.PRE_COMPACTION,
            config={},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=self._registry_with_compaction_check(_block),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = CompactionEvent(
            transcript_path=None, messages=["msg-1", "msg-2"]
        )
        # Observational: no verdict returned; audit written.
        result = await handler.on_pre_compact(event)
        assert result is None
        await asyncio.sleep(0)
        assert tool_ctx.events
        _, ev = tool_ctx.events[0]
        assert ev["stage"] == "pre_compaction"
        assert ev["verdict_action"] == "block"
        assert ev["triggered_rule_ids"] == ["r-comp"]

    @pytest.mark.asyncio
    async def test_allow_verdict_still_publishes_per_rule_audit(self) -> None:
        # Matches V1 pipeline contract: any rule that actually ran
        # produces an audit row, regardless of verdict. This lets ops
        # query "did my rules run" without relying on a rule actually
        # firing a block. Change = visible contract break; regression
        # test needed.
        from rolemesh.safety.types import Verdict

        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            check_id="stub.compact",
            stage=Stage.PRE_COMPACTION,
            config={},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=self._registry_with_compaction_check(
                lambda _ctx: Verdict(action="allow")
            ),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        await handler.on_pre_compact(
            CompactionEvent(
                transcript_path=None, messages=["anything"]
            )
        )
        assert len(tool_ctx.events) == 1
        _, ev = tool_ctx.events[0]
        assert ev["verdict_action"] == "allow"

    @pytest.mark.asyncio
    async def test_rule_for_other_stage_does_not_fire_on_compact(
        self,
    ) -> None:
        # PRE_TOOL_CALL rule must be ignored when the handler is
        # invoked for PRE_COMPACTION — defends against a refactor that
        # accidentally unions stages.
        tool_ctx = _FakeToolCtx()
        rule = make_rule(
            stage=Stage.PRE_TOOL_CALL,
            config={"patterns": {"SSN": True}},
        )
        handler = SafetyHookHandler(
            rules=[rule],
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        await handler.on_pre_compact(
            CompactionEvent(
                transcript_path=None, messages=["SSN 123-45-6789"]
            )
        )
        assert tool_ctx.events == []
