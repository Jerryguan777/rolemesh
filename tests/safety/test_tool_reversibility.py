"""Tests for the tool-reversibility table and resolver.

Pin BUILTIN_REVERSIBILITY as a snapshot so any drive-by change to the
conservative read (Bash = False, Read = True, etc.) surfaces in CI
review rather than silently reclassifying a dangerous tool as safe.
"""

from __future__ import annotations

from rolemesh.safety.tool_reversibility import (
    BUILTIN_REVERSIBILITY,
    get_tool_reversibility,
    resolve_from_full_tool_name,
)


class TestBuiltinTable:
    """Snapshot test. Adding a row is fine; removing or flipping one
    requires a conscious review and should show up in diff."""

    def test_read_only_tools_are_reversible(self) -> None:
        assert BUILTIN_REVERSIBILITY["Read"] is True
        assert BUILTIN_REVERSIBILITY["Grep"] is True
        assert BUILTIN_REVERSIBILITY["Glob"] is True
        assert BUILTIN_REVERSIBILITY["WebFetch"] is True
        assert BUILTIN_REVERSIBILITY["WebSearch"] is True

    def test_mutating_tools_are_not_reversible(self) -> None:
        assert BUILTIN_REVERSIBILITY["Write"] is False
        assert BUILTIN_REVERSIBILITY["Edit"] is False
        assert BUILTIN_REVERSIBILITY["MultiEdit"] is False
        assert BUILTIN_REVERSIBILITY["Bash"] is False
        assert BUILTIN_REVERSIBILITY["NotebookEdit"] is False

    def test_table_is_not_accidentally_empty(self) -> None:
        # Guards against an aggressive refactor that wipes the dict.
        assert len(BUILTIN_REVERSIBILITY) >= 10


class TestGetToolReversibility:
    def test_builtin_tool_resolves_from_table(self) -> None:
        assert get_tool_reversibility("Read") is True
        assert get_tool_reversibility("Write") is False

    def test_builtin_wins_over_mcp_override(self) -> None:
        # Stock tool names have well-known behaviour; a misconfigured
        # MCP server should NOT be able to claim ``Write`` is reversible.
        assert (
            get_tool_reversibility("Write", {"Write": True}) is False
        )

    def test_unknown_tool_with_mcp_map_uses_it(self) -> None:
        assert (
            get_tool_reversibility(
                "some_mcp_tool", {"some_mcp_tool": True}
            )
            is True
        )

    def test_unknown_tool_without_map_falls_back_to_false(self) -> None:
        # Fail-safe default — unknown tool treated as having side
        # effects until proven otherwise.
        assert get_tool_reversibility("brand_new_tool") is False


class TestResolveFromFullToolName:
    def test_mcp_prefixed_name_splits_correctly(self) -> None:
        maps = {"github": {"list_pulls": True, "create_pr": False}}
        assert (
            resolve_from_full_tool_name("mcp__github__list_pulls", maps)
            is True
        )
        assert (
            resolve_from_full_tool_name("mcp__github__create_pr", maps)
            is False
        )

    def test_nested_underscores_in_bare_name_preserved(self) -> None:
        # split("__", 2) keeps the tail intact even with extra __
        maps = {"my_srv": {"compound__bare_name": True}}
        assert (
            resolve_from_full_tool_name(
                "mcp__my_srv__compound__bare_name", maps
            )
            is True
        )

    def test_unknown_server_falls_back_to_bare_name_lookup(self) -> None:
        # Server not in the map but bare name is in the builtin table.
        # Because we split the prefix and only look up the bare name,
        # "Read" inside an unregistered server name still resolves.
        assert (
            resolve_from_full_tool_name(
                "mcp__unknown_server__Read", {}
            )
            is True
        )

    def test_non_mcp_name_uses_builtin_lookup(self) -> None:
        assert resolve_from_full_tool_name("Read") is True
        assert resolve_from_full_tool_name("Bash") is False
