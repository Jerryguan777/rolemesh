"""Tool reversibility table — feeds the pipeline's cost_class x
reversibility matrix (V2 P0.4).

``reversible=True`` means the tool has no persistent side effect: the
agent can invoke it without changing the world. A slow safety check
on a reversible tool would exceed the 100 ms PRE_TOOL_CALL budget for
no benefit (the tool is trivially undoable), so the pipeline rejects
that combination at runtime.

``reversible=False`` means the tool mutates state (file writes, PR
creation, Bash). A slow check here fits within the 2000 ms
irreversible budget because the cost of letting a bad call through is
much higher.

The builtin table covers Claude's stock tool names. External MCP
tools declare their reversibility on ``McpServerConfig.tool_reversibility``
in the coworker configuration; the orchestrator forwards that map
into ``AgentInitData.mcp_servers[i].tool_reversibility`` so the
container's ``ToolContext`` can resolve a decision without consulting
the orchestrator.

Fallback posture: unknown tool → ``False`` (irreversible). This is
fail-safe — a novel tool we've never seen is assumed to have side
effects until proven otherwise. Operators who know better can
declare reversibility on the MCP server config.
"""

from __future__ import annotations

# Stable entries — adding/removing one requires a deliberate review of
# whether the semantics actually match. Do NOT add tools whose
# side-effect profile varies by argument (Bash is technically
# "reversible" for ``ls`` but not ``rm``; we mark it False to match
# the conservative read). A snapshot test pins this table so a
# drive-by change is surfaced in CI.
BUILTIN_REVERSIBILITY: dict[str, bool] = {
    # Read-only
    "Read": True,
    "Grep": True,
    "Glob": True,
    "WebFetch": True,
    "WebSearch": True,
    "TodoRead": True,
    # Mutation / side-effecting
    "Edit": False,
    "Write": False,
    "MultiEdit": False,
    "Bash": False,
    "NotebookEdit": False,
    "TodoWrite": False,
    "Task": False,
}


def get_tool_reversibility(
    tool_name: str,
    mcp_tool_reversibility: dict[str, bool] | None = None,
) -> bool:
    """Resolve reversibility for ``tool_name``.

    Lookup order:
      1. ``BUILTIN_REVERSIBILITY`` — stock Claude tool names win
         even if an MCP server redeclared them, since the stock tools
         have well-known behaviour we don't want operators to override.
      2. ``mcp_tool_reversibility`` — per-server overrides for tools
         that originate in the server (bare tool name, without the
         ``mcp__{server}__`` wrapper).
      3. ``False`` fail-safe default.
    """
    if tool_name in BUILTIN_REVERSIBILITY:
        return BUILTIN_REVERSIBILITY[tool_name]
    if mcp_tool_reversibility and tool_name in mcp_tool_reversibility:
        return mcp_tool_reversibility[tool_name]
    return False


def resolve_from_full_tool_name(
    full_tool_name: str,
    mcp_server_maps: dict[str, dict[str, bool]] | None = None,
) -> bool:
    """Resolve reversibility when given the SDK-mangled full name.

    The Claude SDK advertises MCP tools as ``mcp__{server}__{bare_name}``
    (two underscores on each side). Split it, look up the per-server
    map, then fall through to the bare-name lookup. If the prefix is
    missing we just use the whole string as-is — this path covers the
    stock tools whose names come through unmangled.

    ``mcp_server_maps`` is the server-name → reversibility-dict table
    the container gets from ``AgentInitData.mcp_servers``.
    """
    if full_tool_name.startswith("mcp__") and full_tool_name.count("__") >= 2:
        _, server, bare = full_tool_name.split("__", 2)
        server_map = (mcp_server_maps or {}).get(server)
        return get_tool_reversibility(bare, server_map)
    return get_tool_reversibility(full_tool_name)


__all__ = [
    "BUILTIN_REVERSIBILITY",
    "get_tool_reversibility",
    "resolve_from_full_tool_name",
]
