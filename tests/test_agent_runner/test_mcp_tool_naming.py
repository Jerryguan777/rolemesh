"""The MCP tool-name contract (``pi.mcp_naming``) and its hook-side restore.

Bedrock Converse caps tool names at 64 chars / ``[a-zA-Z0-9_-]``;
``compose_llm_tool_name`` enforces that contract for EVERY provider so
a tool has one identity regardless of which model a coworker runs.

Bug-bait focus:

* ≤64 must hold for ANY input — the whole point of the layer.
* Determinism — the alias must be identical across processes/restarts,
  or approval records and prompt caches split between runs.
* Prefix preservation — ``mcp__{server}__`` is parsed by approval
  policies and reversibility maps; an alias that touches it silently
  detaches every policy from its server.
* Restore — policies and reversibility overrides are written against
  ORIGINAL remote tool names. A missing restore doesn't error; it
  silently skips the approval gate. That is the security-relevant
  failure mode this file exists to pin.
* Collision — sanitise-only renames (``a.b`` -> ``a_b``) must not
  collide with a sibling tool REALLY named ``a_b``.
"""

from __future__ import annotations

import pytest

from agent_runner.hooks.handlers.approval import parse_mcp_tool_name
from pi.mcp_naming import (
    TOOL_NAME_MAX,
    compose_llm_tool_name,
    restore_bare_tool_name,
)

# ---------------------------------------------------------------------------
# compose_llm_tool_name
# ---------------------------------------------------------------------------


def test_compliant_name_passes_through_verbatim() -> None:
    name = compose_llm_tool_name("github", "create_pull_request")
    assert name == "mcp__github__create_pull_request"


def test_long_name_is_capped_at_64_with_prefix_intact() -> None:
    server = "internal_dev_tools"
    remote = "github_search_issues_advanced_filtering_with_extra_options"
    name = compose_llm_tool_name(server, remote)
    assert len(name) <= TOOL_NAME_MAX
    assert name.startswith(f"mcp__{server}__"), (
        "the server prefix is structural (policy matching parses it) "
        "and must never be altered"
    )
    assert name != f"mcp__{server}__{remote}"


def test_alias_is_deterministic() -> None:
    server, remote = "srv", "x" * 100
    assert compose_llm_tool_name(server, remote) == compose_llm_tool_name(
        server, remote
    )


def test_distinct_long_names_get_distinct_aliases() -> None:
    # Two remote tools sharing a 60-char prefix differ only past the
    # truncation point — the hash suffix must keep them apart.
    server = "srv"
    a = compose_llm_tool_name(server, "y" * 60 + "_variant_a")
    b = compose_llm_tool_name(server, "y" * 60 + "_variant_b")
    assert a != b
    assert len(a) <= TOOL_NAME_MAX and len(b) <= TOOL_NAME_MAX


def test_illegal_chars_are_sanitised_and_hash_suffixed() -> None:
    # A sanitise-only rename (``search.users`` -> ``search_users``)
    # could collide with a sibling REALLY named ``search_users``; the
    # hash suffix disambiguates.
    dotted = compose_llm_tool_name("gh", "search.users")
    plain = compose_llm_tool_name("gh", "search_users")
    assert dotted != plain
    assert plain == "mcp__gh__search_users"
    assert "." not in dotted


def test_unfittable_server_name_raises() -> None:
    # A server name so long no tool segment fits — impossible after the
    # MCPServerCreate 20-char validator, so fail loud, don't mangle.
    with pytest.raises(ValueError, match="too long"):
        compose_llm_tool_name("s" * 60, "tool")


# ---------------------------------------------------------------------------
# Hook-side restore — parse_mcp_tool_name is the chokepoint both the
# approval handler and the safety bridge resolve names through.
# ---------------------------------------------------------------------------


def test_parse_restores_original_name_for_alias() -> None:
    server = "linear_workspace"
    remote = "update_issue_with_comment_and_status_transition_extras"
    alias = compose_llm_tool_name(server, remote)
    assert alias != f"mcp__{server}__{remote}", "test premise: alias expected"

    parsed = parse_mcp_tool_name(alias)
    assert parsed == (server, remote), (
        "an aliased tool must match policies written against the "
        "ORIGINAL remote name — a miss here silently skips the "
        "approval gate"
    )


def test_parse_is_identity_for_unaliased_names() -> None:
    assert parse_mcp_tool_name("mcp__github__create_pr") == (
        "github",
        "create_pr",
    )


def test_restore_bare_is_identity_when_no_alias_registered() -> None:
    assert restore_bare_tool_name("nosuch", "tool_x") == "tool_x"


def test_reversibility_resolves_via_original_name() -> None:
    """ToolContext.get_tool_reversibility must see the ORIGINAL name.

    The per-server override map is configured against remote tool
    names; without the restore, an aliased tool always falls through
    to the fail-safe ``False`` and e.g. a read-only tool gets the
    irreversible-tool treatment.
    """
    from agent_runner.tools.context import ToolContext

    server = "crm_backend"
    remote = "list_customer_accounts_with_pagination_and_field_selection"
    alias = compose_llm_tool_name(server, remote)
    assert alias != f"mcp__{server}__{remote}", "test premise: alias expected"

    ctx = ToolContext(
        js=None,  # type: ignore[arg-type]
        nc=None,  # type: ignore[arg-type]
        job_id="j",
        chat_jid="jid",
        group_folder="g",
        permissions={},
        tenant_id="t",
        coworker_id="c",
        conversation_id="conv",
        mcp_tool_reversibility={server: {remote: True}},
    )
    assert ctx.get_tool_reversibility(alias) is True
    # Fail-safe still holds for genuinely unknown tools.
    assert ctx.get_tool_reversibility(f"mcp__{server}__unknown") is False
