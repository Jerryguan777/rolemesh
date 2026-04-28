"""Pre-flight check: user-mode MCP servers require --user.

Bug 7 — without ``--user``, eval used to set ``user_id=""`` on the
AgentInput, which made ``X-RoleMesh-User-Id`` go unset, the credential
proxy skipped OIDC bearer injection, and the upstream MCP server
rejected ``initialize``. Bug 9 then made Claude SDK hang forever on
that error. Fail-loud at run start — before any container spawns —
so the operator gets a clear actionable error instead of every sample
silently timing out.
"""

from __future__ import annotations

from typing import Any

from rolemesh.core.types import McpServerConfig
from rolemesh.evaluation.cli import _user_mode_mcp_servers


class _FakeCoworker:
    """Just enough surface for ``_user_mode_mcp_servers`` — avoids
    standing up a full Coworker dataclass with all its required fields.
    """

    def __init__(self, tools: list[McpServerConfig]) -> None:
        self.tools = tools


def _mcp(name: str, auth_mode: str) -> McpServerConfig:
    return McpServerConfig(
        name=name, type="http", url="http://x", auth_mode=auth_mode,
    )


def test_lists_user_mode_servers() -> None:
    """``user`` and ``both`` both flag — ``service`` is safe."""
    cw = _FakeCoworker(tools=[
        _mcp("svc-a", "service"),
        _mcp("usr-a", "user"),
        _mcp("usr-b", "user"),
        _mcp("both-a", "both"),
    ])
    offending = _user_mode_mcp_servers(cw)
    assert sorted(offending) == ["both-a", "usr-a", "usr-b"]


def test_no_user_mode_servers_returns_empty() -> None:
    """Coworker with only service-mode MCP — eval must run without
    --user. Mutation guard against returning all servers regardless
    of mode."""
    cw = _FakeCoworker(tools=[
        _mcp("svc-a", "service"),
        _mcp("svc-b", "service"),
    ])
    assert _user_mode_mcp_servers(cw) == []


def test_no_tools_returns_empty() -> None:
    """A coworker with no MCP tools at all — empty list, no fail."""
    cw = _FakeCoworker(tools=[])
    assert _user_mode_mcp_servers(cw) == []


def test_tools_none_returns_empty() -> None:
    """``coworker.tools`` may be ``None`` on some load paths
    (defensive). Off-by-one guard: ``None or []`` mustn't crash."""

    class _NoToolsCoworker:
        tools: Any = None

    assert _user_mode_mcp_servers(_NoToolsCoworker()) == []
