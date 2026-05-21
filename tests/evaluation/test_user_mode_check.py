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

from rolemesh.core.types import McpServerConfig
from rolemesh.evaluation.cli import _user_mode_mcp_servers


def _mcp(name: str, auth_mode: str) -> McpServerConfig:
    return McpServerConfig(
        name=name, type="http", url="http://x", auth_mode=auth_mode,
    )


def test_lists_user_mode_servers() -> None:
    """``user`` and ``both`` both flag — ``service`` is safe."""
    configs = [
        _mcp("svc-a", "service"),
        _mcp("usr-a", "user"),
        _mcp("usr-b", "user"),
        _mcp("both-a", "both"),
    ]
    offending = _user_mode_mcp_servers(configs)
    assert sorted(offending) == ["both-a", "usr-a", "usr-b"]


def test_no_user_mode_servers_returns_empty() -> None:
    """Coworker with only service-mode MCP — eval must run without
    --user. Mutation guard against returning all servers regardless
    of mode."""
    configs = [
        _mcp("svc-a", "service"),
        _mcp("svc-b", "service"),
    ]
    assert _user_mode_mcp_servers(configs) == []


def test_no_tools_returns_empty() -> None:
    """A coworker with no MCP bindings at all — empty list, no fail."""
    assert _user_mode_mcp_servers([]) == []


def test_none_argument_returns_empty() -> None:
    """``None`` is tolerated for fixtures that hand a stub coworker
    whose binding lookup raised. The pre-flight check must never
    crash on a malformed input — that would replace a clear error
    with a confusing traceback."""
    assert _user_mode_mcp_servers(None) == []
