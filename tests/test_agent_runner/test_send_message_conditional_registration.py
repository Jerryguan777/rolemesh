"""Conditional registration of the send_message tool by container kind.

Per the nanoclaw-derived design intent (see
docs/safety/toggle-experiments.md and the commit introducing this
test), ``send_message`` is a scheduled-task notification output —
NOT a general reply channel. To prevent LLMs (especially Claude,
whose tool-use bias is high) from misusing it as the user-facing
reply path during interactive conversations, we conditionally exclude
the tool from the agent's tool registry when ``is_scheduled_task=False``.

Without this gate, prompt-engineering alone (the tool's own
description saying "DO NOT call this in interactive conversations")
fails empirically — Claude with polluted session history routinely
ignores the warning and calls the tool, then the orchestrator drops
the IPC and the user sees no reply (the natural-output path is
short / cryptic when Claude routes the real content through the tool).

These tests pin the contract:
  - Interactive container (is_scheduled_task=False) → send_message
    NOT in tool list. Claude / Pi cannot pick it from a list it can't
    see.
  - Scheduled-task container (is_scheduled_task=True) → send_message
    IS in tool list. The original nanoclaw use case (cron-task →
    push result to group) keeps working.
  - Other tools (schedule_task, list_tasks, ...) are present in BOTH
    cases — they're not coupled to the gate.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

# Stub claude_agent_sdk before importing claude_adapter. We populate
# sys.modules ADDITIVELY because other test files in this directory
# also stub claude_agent_sdk for their own purposes; replacing
# wholesale would clobber them. Each test restores via monkeypatch
# so cross-file state pollution is bounded to one test scope.

_sdk = sys.modules.get("claude_agent_sdk")
if _sdk is None:
    _sdk = types.ModuleType("claude_agent_sdk")
    sys.modules["claude_agent_sdk"] = _sdk


def _ensure(attr: str, value: Any) -> None:
    if not hasattr(_sdk, attr):
        setattr(_sdk, attr, value)


_ensure(
    "ClaudeAgentOptions",
    type("ClaudeAgentOptions", (), {"__init__": lambda self, **kw: None}),
)
_ensure(
    "HookMatcher",
    type(
        "HookMatcher",
        (),
        {"__init__": lambda self, **kw: setattr(self, "hooks", kw.get("hooks"))},
    ),
)
_ensure("ToolUseBlock", type("ToolUseBlock", (), {}))
_ensure("query", lambda **kw: iter(()))
# Provide a placeholder for ``tool`` and ``create_sdk_mcp_server`` only
# when they are missing entirely — other test files set them to a
# different stub for their own assertions, and we don't want to
# clobber that here. We'll inject our recording versions via
# monkeypatch INSIDE each test so they don't bleed across tests.
_ensure("tool", lambda *a, **kw: (lambda fn: fn))
_ensure("create_sdk_mcp_server", lambda **kw: object())


from agent_runner.tools import claude_adapter  # noqa: E402
from agent_runner.tools.context import ToolContext  # noqa: E402


def _ctx() -> ToolContext:
    return ToolContext(
        js=None,  # type: ignore[arg-type]
        job_id="test-job",
        chat_jid="test-chat",
        group_folder="test-group",
        permissions={"task_schedule": True},
        tenant_id="t",
        coworker_id="cw",
        conversation_id="conv",
    )


def _recording_tool(name: str, description: str, params: Any) -> Any:
    """A recording @tool decorator. The returned object exposes
    ``_tool_name`` for tests to introspect."""

    def decorator(fn: Any) -> Any:
        return types.SimpleNamespace(
            _tool_name=name,
            _tool_description=description,
            _fn=fn,
        )

    return decorator


def _recording_create_sdk_mcp_server(name: str, *, tools: list[Any]) -> Any:
    """Returns a SimpleNamespace exposing the registered tools so
    tests can assert on the list contents."""
    return types.SimpleNamespace(name=name, tools=tools)


@pytest.fixture
def claude_recording_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch claude_adapter's local references to ``tool`` and
    ``create_sdk_mcp_server`` to recording versions for this test
    only. Done at the claude_adapter module level (NOT sys.modules)
    because claude_adapter's ``from claude_agent_sdk import ...``
    has already copied the names locally; mutating sys.modules later
    has no effect on those local bindings."""
    monkeypatch.setattr(claude_adapter, "tool", _recording_tool)
    monkeypatch.setattr(
        claude_adapter, "create_sdk_mcp_server", _recording_create_sdk_mcp_server
    )


def _tool_names_claude(server: Any) -> list[str]:
    return [getattr(t, "_tool_name") for t in server.tools]


def test_claude_interactive_container_excludes_send_message(
    claude_recording_sdk: None,
) -> None:
    """The whole point of the gate: an interactive container must
    NOT expose send_message. Claude can't misuse a tool it doesn't
    know exists."""
    server = claude_adapter.create_rolemesh_mcp_server(
        _ctx(), register_send_message=False
    )
    names = _tool_names_claude(server)
    assert "send_message" not in names, (
        "send_message leaked into interactive Claude container — "
        "it would be misused as the reply channel."
    )


def test_claude_scheduled_task_container_includes_send_message(
    claude_recording_sdk: None,
) -> None:
    """Original nanoclaw use case: a scheduled task pushes its result
    to the group via send_message. Container is launched with
    is_scheduled_task=True and the tool MUST be available."""
    server = claude_adapter.create_rolemesh_mcp_server(
        _ctx(), register_send_message=True
    )
    names = _tool_names_claude(server)
    assert "send_message" in names, (
        "send_message missing from scheduled-task Claude container — "
        "scheduled tasks have no other path to push results to the group."
    )


def test_claude_other_tools_unaffected_by_gate(
    claude_recording_sdk: None,
) -> None:
    """The gate only affects send_message. schedule_task, list_tasks,
    etc. must be present in both interactive and scheduled-task
    containers — they have their own purposes."""
    expected_always_present = {
        "schedule_task",
        "list_tasks",
        "pause_task",
        "resume_task",
        "cancel_task",
        "update_task",
    }
    interactive = _tool_names_claude(
        claude_adapter.create_rolemesh_mcp_server(_ctx(), register_send_message=False)
    )
    scheduled = _tool_names_claude(
        claude_adapter.create_rolemesh_mcp_server(_ctx(), register_send_message=True)
    )
    assert expected_always_present <= set(interactive)
    assert expected_always_present <= set(scheduled)


def test_claude_default_is_no_send_message(
    claude_recording_sdk: None,
) -> None:
    """Default arg should be safe-by-default: omitting the kwarg means
    no send_message. This protects against forgetting to thread the
    flag through new call sites."""
    server = claude_adapter.create_rolemesh_mcp_server(_ctx())
    assert "send_message" not in _tool_names_claude(server)


# --- Pi adapter -------------------------------------------------------------
#
# Pi imports pull in optional deps (boto3, partial_json_parser,
# google.genai). importorskip is called per-test rather than at module
# level so the Claude-side tests above still run in a minimal dev venv.


def _tool_names_pi(tools: list[Any]) -> list[str]:
    """Pi AgentTool exposes name via .name attribute (or similar)."""
    out: list[str] = []
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "_tool_name", None) or getattr(t, "tool_name", None)
        if name is None and hasattr(t, "_name"):
            name = t._name
        if name is not None:
            out.append(name)
    return out


def test_pi_interactive_container_excludes_send_message() -> None:
    pi_adapter = pytest.importorskip(
        "agent_runner.tools.pi_adapter",
        reason="Pi adapter deps not installed in this env",
    )
    tools = pi_adapter.create_rolemesh_tools(_ctx(), register_send_message=False)
    assert "send_message" not in _tool_names_pi(tools)


def test_pi_scheduled_task_container_includes_send_message() -> None:
    pi_adapter = pytest.importorskip(
        "agent_runner.tools.pi_adapter",
        reason="Pi adapter deps not installed in this env",
    )
    tools = pi_adapter.create_rolemesh_tools(_ctx(), register_send_message=True)
    assert "send_message" in _tool_names_pi(tools)


def test_pi_default_is_no_send_message() -> None:
    """Same safe-by-default contract as Claude side."""
    pi_adapter = pytest.importorskip(
        "agent_runner.tools.pi_adapter",
        reason="Pi adapter deps not installed in this env",
    )
    tools = pi_adapter.create_rolemesh_tools(_ctx())
    assert "send_message" not in _tool_names_pi(tools)
