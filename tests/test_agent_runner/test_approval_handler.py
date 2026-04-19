"""Tests for ApprovalHookHandler.

Adversarial set:
  - Built-in rolemesh tools must pass through even when a policy exists
    for their server name (otherwise submit_proposal deadlocks itself).
  - Non-MCP tool names (Bash, Read) must pass through.
  - Malformed mcp__ names (e.g. missing tool segment) must pass through
    without raising — a crash here would take down the entire turn.
  - On match, the handler must publish the NATS task AND return a block
    verdict. Publishing without blocking (or vice versa) leaves the user
    unable to audit or acts unilaterally.
  - Published payload must include the jobId so the orchestrator can
    correlate the approval with its originating agent run.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.hooks.handlers.approval import ApprovalHookHandler
from agent_runner.tools.context import ToolContext


@dataclass
class _Pub:
    subject: str
    data: dict[str, Any]


class _FakeJS:
    def __init__(self) -> None:
        self.publishes: list[_Pub] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publishes.append(_Pub(subject=subject, data=json.loads(data)))


def _ctx(user_id: str = "user-x", job_id: str = "job-1") -> tuple[ToolContext, _FakeJS]:
    js = _FakeJS()
    return (
        ToolContext(
            js=js,  # type: ignore[arg-type]
            job_id=job_id,
            chat_jid="chat",
            group_folder="grp",
            permissions={},
            tenant_id="tenant-1",
            coworker_id="cw-1",
            conversation_id="conv-1",
            user_id=user_id,
        ),
        js,
    )


def _policy(
    *,
    server: str = "erp",
    tool: str = "refund",
    cond: dict[str, object] | None = None,
    priority: int = 0,
    policy_id: str = "policy-abc-123",
) -> dict[str, object]:
    return {
        "id": policy_id,
        "enabled": True,
        "mcp_server_name": server,
        "tool_name": tool,
        "condition_expr": cond or {"always": True},
        "priority": priority,
        "updated_at": "2026-04-10T00:00:00+00:00",
        "auto_expire_minutes": 60,
    }


# ---------------------------------------------------------------------------
# Non-gated tools must pass through
# ---------------------------------------------------------------------------


class TestPassthrough:
    async def test_non_mcp_tool_ignored(self) -> None:
        ctx, js = _ctx()
        handler = ApprovalHookHandler([_policy()], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(tool_name="Bash", tool_input={"command": "ls"})
        )
        assert verdict is None
        await asyncio.sleep(0.05)
        assert js.publishes == []

    async def test_rolemesh_builtin_never_gated(self) -> None:
        # Even with a policy that declares mcp_server_name="rolemesh" and
        # tool_name="*", the handler must let rolemesh builtins through.
        ctx, js = _ctx()
        broad = _policy(server="rolemesh", tool="*")
        handler = ApprovalHookHandler([broad], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="mcp__rolemesh__submit_proposal",
                tool_input={"actions": [], "rationale": "x"},
            )
        )
        assert verdict is None
        await asyncio.sleep(0.05)
        assert js.publishes == []

    async def test_malformed_mcp_name_does_not_raise(self) -> None:
        # e.g. "mcp__onlyserver" with no tool segment.
        ctx, js = _ctx()
        handler = ApprovalHookHandler([_policy()], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(tool_name="mcp__onlyserver", tool_input={})
        )
        assert verdict is None
        await asyncio.sleep(0.05)
        assert js.publishes == []

    async def test_unrelated_mcp_server_ignored(self) -> None:
        ctx, js = _ctx()
        handler = ApprovalHookHandler([_policy(server="erp")], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="mcp__crm__list_contacts", tool_input={}
            )
        )
        assert verdict is None
        await asyncio.sleep(0.05)
        assert js.publishes == []


# ---------------------------------------------------------------------------
# Condition-gated matches
# ---------------------------------------------------------------------------


class TestMatching:
    async def test_block_when_condition_matches(self) -> None:
        ctx, _js = _ctx()
        p = _policy(cond={"field": "amount", "op": ">", "value": 1000})
        handler = ApprovalHookHandler([p], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="mcp__erp__refund",
                tool_input={"amount": 5000, "order_id": "o1"},
            )
        )
        assert verdict is not None
        assert verdict.block is True
        assert verdict.reason is not None and "approval" in verdict.reason.lower()

    async def test_pass_through_when_condition_does_not_match(self) -> None:
        ctx, js = _ctx()
        p = _policy(cond={"field": "amount", "op": ">", "value": 1000})
        handler = ApprovalHookHandler([p], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="mcp__erp__refund",
                tool_input={"amount": 50, "order_id": "o2"},
            )
        )
        assert verdict is None
        await asyncio.sleep(0.05)
        assert js.publishes == []

    async def test_wildcard_tool_on_server_matches(self) -> None:
        ctx, _js = _ctx()
        p = _policy(tool="*")
        handler = ApprovalHookHandler([p], ctx)
        verdict = await handler.on_pre_tool_use(
            ToolCallEvent(tool_name="mcp__erp__any_destructive_tool", tool_input={})
        )
        assert verdict is not None and verdict.block is True


# ---------------------------------------------------------------------------
# NATS payload contract
# ---------------------------------------------------------------------------


class TestPublishedPayload:
    async def test_publishes_auto_approval_with_identity(self) -> None:
        ctx, js = _ctx(user_id="user-99", job_id="job-9")
        p = _policy(policy_id="00000000-0000-0000-0000-000000000001")
        handler = ApprovalHookHandler([p], ctx)
        await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="mcp__erp__refund", tool_input={"amount": 500}
            )
        )
        await asyncio.sleep(0.05)
        assert len(js.publishes) == 1
        pub = js.publishes[0]
        assert pub.subject == "agent.job-9.tasks"
        assert pub.data["type"] == "auto_approval_request"
        assert pub.data["mcp_server_name"] == "erp"
        assert pub.data["tool_name"] == "refund"
        assert pub.data["tool_params"] == {"amount": 500}
        assert pub.data["userId"] == "user-99"
        assert pub.data["tenantId"] == "tenant-1"
        assert pub.data["coworkerId"] == "cw-1"
        assert pub.data["conversationId"] == "conv-1"
        assert pub.data["policy_id"] == "00000000-0000-0000-0000-000000000001"
        # action_hash is hex sha256
        assert isinstance(pub.data["action_hash"], str)
        assert len(pub.data["action_hash"]) == 64


# ---------------------------------------------------------------------------
# Priority selection when multiple policies match
# ---------------------------------------------------------------------------


class TestPrioritySelection:
    async def test_highest_priority_policy_id_used_in_payload(self) -> None:
        ctx, js = _ctx()
        low = _policy(policy_id="policy-low", priority=0)
        high = _policy(policy_id="policy-high", priority=10)
        handler = ApprovalHookHandler([low, high], ctx)
        await handler.on_pre_tool_use(
            ToolCallEvent(tool_name="mcp__erp__refund", tool_input={})
        )
        await asyncio.sleep(0.05)
        assert js.publishes[0].data["policy_id"] == "policy-high"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
