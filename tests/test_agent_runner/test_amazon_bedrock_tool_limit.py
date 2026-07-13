"""Bedrock Converse tool-name length contract (provider-side guard).

Bedrock's Converse API caps tool names at 64 characters with pattern
``[a-zA-Z0-9_-]+``; Anthropic native and OpenAI accept up to 128.
MCP tool names are normalised to this contract upstream for every
provider (``pi.mcp_naming`` — with a dispatch-safe alias registry, see
``test_mcp_tool_naming``), so this validator is the LAST-RESORT guard:
anything tripping it is a mis-named non-MCP custom tool, and raising
with the offending name beats an opaque AWS ValidationException.
"""

from __future__ import annotations

import pytest

# pi.ai providers depend on extras (boto3 etc.). If the dev env
# doesn't have them installed, skip cleanly rather than blowing up
# at import time. The container that actually runs Pi backend has
# boto3 baked in.
boto3 = pytest.importorskip("boto3")

from pi.ai.providers.amazon_bedrock import _convert_tool_config  # noqa: E402
from pi.ai.types import Tool  # noqa: E402


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
    # NOTE: the previous literal here was exactly 64 chars — the *accepted*
    # boundary — so `len(bad) > 64` failed before the real check ran. This
    # test was failing under the (now-fixed) vacuously-green CI.
    bad = "mcp__internal_dev_tools_v2__github_search_issues_advanced_filtering"
    assert len(bad) > 64, f"test setup bug: bad={len(bad)} chars, need >64"
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


def test_starting_digit_is_accepted() -> None:
    # AWS's documented pattern is ``[a-zA-Z0-9_-]+`` — no leading-letter
    # requirement. An earlier revision of our validator added one and
    # mis-rejected valid names like this.
    cfg = _convert_tool_config([_tool("2fa_setup")], "auto")
    assert cfg is not None


def test_starting_underscore_is_accepted() -> None:
    cfg = _convert_tool_config([_tool("_private_tool")], "auto")
    assert cfg is not None


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
