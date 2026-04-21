"""Tests for SafetyHookHandler.

Focus: the adapter layer between agent_runner hook events and the
pipeline. Uses a minimal fake ToolContext so tests stay DB/NATS-free.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_default_registry

from .conftest import make_rule


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

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))


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
            registry=build_default_registry(),
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
            registry=build_default_registry(),
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
            registry=build_default_registry(),
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
            registry=build_default_registry(),
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
            registry=build_default_registry(),
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
