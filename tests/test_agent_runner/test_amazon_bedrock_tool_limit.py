"""Bedrock Converse tool-name length contract.

Bedrock's Converse API caps tool names at 64 characters; Anthropic
native and OpenAI accept up to 128. The provider raises rather than
silently truncating because a hidden mapping layer would defer the
failure to a confusing point downstream — the agent would call a
tool name that no longer matches the registry it built locally.
"""

from __future__ import annotations

import pytest

# pi.ai providers depend on extras (boto3 etc.). If the dev env
# doesn't have them installed, skip cleanly rather than blowing up
# at import time. The container that actually runs Pi backend has
# boto3 baked in.
boto3 = pytest.importorskip("boto3")  # noqa: F841

from pi.ai.providers.amazon_bedrock import _convert_tool_config
from pi.ai.tool import Tool


def _tool(name: str) -> Tool:
    return Tool(name=name, description="x", parameters={"type": "object"})


def test_short_name_is_accepted() -> None:
    cfg = _convert_tool_config([_tool("ok_short_name")], "auto")
    assert cfg is not None
    assert cfg["tools"][0]["toolSpec"]["name"] == "ok_short_name"


def test_exactly_64_chars_is_accepted() -> None:
    name = "a" * 64
    cfg = _convert_tool_config([_tool(name)], "auto")
    assert cfg is not None
    assert cfg["tools"][0]["toolSpec"]["name"] == name


def test_65_chars_raises_with_specific_name() -> None:
    bad = "a" * 65
    with pytest.raises(ValueError) as excinfo:
        _convert_tool_config([_tool(bad)], "auto")
    msg = str(excinfo.value)
    # Error must name the offender so a CI log surfaces actionable
    # context — the operator needs to know WHICH tool to rename.
    assert bad in msg
    assert "64" in msg


def test_realistic_long_mcp_name_raises() -> None:
    # The exact failure mode this guard exists for: an MCP tool name
    # with a long server prefix that's fine on Anthropic-direct but
    # exceeds Bedrock's limit. Should fail loud, not truncate.
    bad = "mcp__internal_dev_tools_v2__github_search_issues_advanced_filter"
    assert len(bad) > 64
    with pytest.raises(ValueError, match="Bedrock Converse"):
        _convert_tool_config([_tool(bad)], "auto")


# ---------------------------------------------------------------------------
# Character set (added P2-#3): Bedrock requires
# ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ — a starting letter, then letters /
# digits / underscore / hyphen only. Other LLM providers accept a
# wider charset; we validate ONLY for Bedrock to keep the rolemesh
# error story specific to "this is a Bedrock constraint".
# ---------------------------------------------------------------------------


def test_dot_in_name_raises() -> None:
    # ``.`` is common in MCP server names (``github.search``) but
    # Bedrock rejects it. Surface the error in our layer rather
    # than as a 400 ValidationException from AWS.
    with pytest.raises(ValueError, match="Bedrock Converse"):
        _convert_tool_config([_tool("github.search")], "auto")


def test_slash_in_name_raises() -> None:
    with pytest.raises(ValueError, match="Bedrock Converse"):
        _convert_tool_config([_tool("scope/tool")], "auto")


def test_at_sign_in_name_raises() -> None:
    with pytest.raises(ValueError, match="Bedrock Converse"):
        _convert_tool_config([_tool("scope@v1")], "auto")


def test_starting_digit_raises() -> None:
    # First char must be a letter — ``[a-zA-Z]``. Names like
    # ``2fa_setup`` would slip past a length-only check.
    with pytest.raises(ValueError, match="Bedrock Converse"):
        _convert_tool_config([_tool("2fa_setup")], "auto")


def test_starting_underscore_raises() -> None:
    with pytest.raises(ValueError, match="Bedrock Converse"):
        _convert_tool_config([_tool("_private_tool")], "auto")


def test_double_underscore_mcp_style_is_accepted() -> None:
    # Real-world: rolemesh's MCP tools use ``mcp__server__tool``.
    # Double underscore must NOT trip the validator — that pattern
    # is what makes operator-side short-prefix scheme practical.
    cfg = _convert_tool_config([_tool("mcp__github__search")], "auto")
    assert cfg is not None
    assert cfg["tools"][0]["toolSpec"]["name"] == "mcp__github__search"


def test_hyphenated_name_is_accepted() -> None:
    # Common in OpenAPI-derived MCP servers.
    cfg = _convert_tool_config([_tool("get-user-by-id")], "auto")
    assert cfg is not None


def test_no_tools_short_circuits_without_check() -> None:
    # Empty tools list => no validation runs; mirrors the upstream
    # ``if not tools or tool_choice == "none"`` early return.
    assert _convert_tool_config([], "auto") is None
    assert _convert_tool_config(None, "auto") is None
    assert _convert_tool_config([_tool("x")], "none") is None
