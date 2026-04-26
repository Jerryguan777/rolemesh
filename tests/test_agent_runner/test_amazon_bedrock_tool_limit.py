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


def test_no_tools_short_circuits_without_check() -> None:
    # Empty tools list => no validation runs; mirrors the upstream
    # ``if not tools or tool_choice == "none"`` early return.
    assert _convert_tool_config([], "auto") is None
    assert _convert_tool_config(None, "auto") is None
    assert _convert_tool_config([_tool("x")], "none") is None
