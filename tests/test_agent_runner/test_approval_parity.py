"""Parity test: ApprovalHookHandler produces the same block verdict and
the same NATS publish whether it runs behind the Claude or Pi bridge.

We reuse the stub-claude-sdk pattern from test_hook_parity.py so this
test module can import ``claude_backend`` without a real SDK installed.

What counts as "parity":
  - Identical block reason string surfaced to the user on match.
  - Identical NATS subject and ``auto_approval_request`` payload shape.
  - On a non-matching tool, neither bridge produces a block (both
    return an empty-shaped response).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

# Stub the Claude SDK symbols that claude_backend imports at module level.
# Mirrors test_hook_parity.py.
_fake_sdk = types.ModuleType("claude_agent_sdk")
_fake_sdk.ClaudeAgentOptions = type(  # type: ignore[attr-defined]
    "ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None}
)
_fake_sdk.HookMatcher = type(  # type: ignore[attr-defined]
    "HookMatcher",
    (),
    {"__init__": lambda self, **kw: setattr(self, "hooks", kw.get("hooks"))},
)
_fake_sdk.ToolUseBlock = type("ToolUseBlock", (), {})  # type: ignore[attr-defined]
_fake_sdk.query = lambda **kw: iter(())  # type: ignore[attr-defined]
_fake_sdk.create_sdk_mcp_server = lambda **kw: object()  # type: ignore[attr-defined]
_fake_sdk.tool = lambda *a, **kw: (lambda fn: fn)  # type: ignore[attr-defined]
sys.modules.setdefault("claude_agent_sdk", _fake_sdk)


from agent_runner import claude_backend, pi_backend  # noqa: E402
from agent_runner.hooks import HookRegistry  # noqa: E402
from agent_runner.hooks.handlers.approval import ApprovalHookHandler  # noqa: E402
from agent_runner.tools.context import ToolContext  # noqa: E402


class _RecordingHookMatcher:
    def __init__(self, hooks: list[Any] | None = None, **_kw: Any) -> None:
        self.hooks = list(hooks) if hooks else []


claude_backend.HookMatcher = _RecordingHookMatcher  # type: ignore[assignment]


@dataclass
class _Pub:
    subject: str
    data: dict[str, Any]


class _FakeJS:
    def __init__(self) -> None:
        self.publishes: list[_Pub] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publishes.append(_Pub(subject=subject, data=json.loads(data)))


def _policy() -> dict[str, Any]:
    return {
        "id": "parity-policy",
        "enabled": True,
        "mcp_server_name": "erp",
        "tool_name": "refund",
        "condition_expr": {"field": "amount", "op": ">", "value": 1000},
        "priority": 0,
        "updated_at": "2026-04-01T00:00:00+00:00",
        "auto_expire_minutes": 60,
    }


def _setup_registry() -> tuple[HookRegistry, _FakeJS]:
    js = _FakeJS()
    ctx = ToolContext(
        js=js,  # type: ignore[arg-type]
        job_id="job-P",
        chat_jid="chat",
        group_folder="grp",
        permissions={},
        tenant_id="tenant-1",
        coworker_id="cw-1",
        conversation_id="conv-1",
        user_id="user-1",
    )
    registry = HookRegistry()
    registry.register(ApprovalHookHandler([_policy()], ctx))
    return registry, js


def _claude_hooks(registry: HookRegistry) -> dict[str, Any]:
    matchers = claude_backend._build_hook_callbacks(registry)
    return {event: matchers[event][0].hooks[0] for event in matchers}


def _pi_handlers(registry: HookRegistry) -> dict[str, Any]:
    ext = pi_backend._build_bridge_extension(registry)
    return {event: ext.handlers[event][0] for event in ext.handlers}


# ---------------------------------------------------------------------------
# Parity on a MATCHING external MCP call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_both_backends_block_matching_call(backend: str) -> None:
    registry, js = _setup_registry()
    tool_input = {"amount": 5000, "order_id": "o1"}

    if backend == "claude":
        cb = _claude_hooks(registry)["PreToolUse"]
        result = await cb(
            {"tool_name": "mcp__erp__refund", "tool_input": tool_input},
            "call-id",
            None,
        )
        assert (
            result["hookSpecificOutput"]["permissionDecision"] == "deny"
        ), "Claude bridge must deny a policy-matched external MCP call"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    else:
        from pi.coding_agent.core.extensions.types import ToolCallEvent as PiEvt

        cb = _pi_handlers(registry)["tool_call"]
        result = await cb(
            PiEvt(
                tool_name="mcp__erp__refund",
                tool_call_id="call-id",
                input=tool_input,
            ),
            None,
        )
        assert result == {
            "block": True,
            "reason": result["reason"],  # type: ignore[index]
        }, "Pi bridge must emit a block verdict when a policy matches"
        reason = result["reason"]  # type: ignore[index]

    # Same reason text — the user-visible explanation must not diverge
    # between backends (this is how the agent will report back).
    assert "approval" in reason.lower()
    assert "submit_proposal" in reason

    # Both bridges must have triggered the NATS auto_approval_request.
    await asyncio.sleep(0.05)
    assert len(js.publishes) == 1, (
        f"{backend}: expected exactly one auto_approval_request publish"
    )
    payload = js.publishes[0].data
    assert payload["type"] == "auto_approval_request"
    assert payload["mcp_server_name"] == "erp"
    assert payload["tool_name"] == "refund"
    assert payload["tool_params"] == tool_input
    assert payload["policy_id"] == "parity-policy"


# ---------------------------------------------------------------------------
# Parity on a NON-matching external MCP call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["claude", "pi"])
async def test_both_backends_allow_non_matching_call(backend: str) -> None:
    registry, js = _setup_registry()
    small_amount = {"amount": 1, "order_id": "o2"}

    if backend == "claude":
        cb = _claude_hooks(registry)["PreToolUse"]
        result = await cb(
            {"tool_name": "mcp__erp__refund", "tool_input": small_amount},
            "id",
            None,
        )
        assert result == {}, "Claude bridge must emit empty dict when not blocked"
    else:
        from pi.coding_agent.core.extensions.types import ToolCallEvent as PiEvt

        cb = _pi_handlers(registry)["tool_call"]
        result = await cb(
            PiEvt(
                tool_name="mcp__erp__refund",
                tool_call_id="id",
                input=small_amount,
            ),
            None,
        )
        assert result is None, "Pi bridge must return None when not blocked"

    await asyncio.sleep(0.05)
    assert js.publishes == [], (
        f"{backend}: no auto_approval_request should be published when the "
        f"condition does not match"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
