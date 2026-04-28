"""Validators and frontmatter helpers for the skills feature.

These run without a database. The DB-side mirror tests live in
tests/db/test_skills.py — that file proves the CHECK constraints
hold even when callers bypass these validators (defense in depth).
"""

from __future__ import annotations

import pytest

from rolemesh.core.skills import (
    DESCRIPTION_MAX_LENGTH,
    DESCRIPTION_MIN_LENGTH,
    SkillValidationError,
    merge_frontmatter_for_backend,
    parse_inbound_skill_md,
    serialize_skill_md,
    validate_skill_file_path,
    validate_skill_name,
)


# ---------------------------------------------------------------------------
# Name validator
# ---------------------------------------------------------------------------


def test_skill_name_accepts_canonical() -> None:
    validate_skill_name("code-review")
    validate_skill_name("a")
    validate_skill_name("Bug_Triage_42")


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "1starts-with-digit",
        "-leading-dash",
        "_leading-underscore",
        "has space",
        "has/slash",
        "..",
        "a" * 65,  # too long (regex caps at 64)
        "name.with.dot",
    ],
)
def test_skill_name_rejects_invalid(bad_name: str) -> None:
    with pytest.raises(SkillValidationError):
        validate_skill_name(bad_name)


# ---------------------------------------------------------------------------
# Path validator
# ---------------------------------------------------------------------------


def test_path_accepts_canonical() -> None:
    validate_skill_file_path("SKILL.md")
    validate_skill_file_path("reference.md")
    validate_skill_file_path("scripts/helper.py")
    validate_skill_file_path("a/b/c/d.txt")
    validate_skill_file_path("name_with-dashes.md")


@pytest.mark.parametrize(
    "bad_path",
    [
        "",  # empty
        "/abs",  # leading slash
        "./SKILL.md",  # leading-dot segment
        "SKILL.md/",  # trailing slash
        "..",  # bare dot-dot
        ".",  # bare dot
        "a/..",  # trailing dot-dot segment
        "a/../b",  # midpoint dot-dot
        "a/./b",  # midpoint dot
        "back\\slash",  # backslash
        "with space.md",  # whitespace
        "double//slash.md",  # empty segment
    ],
)
def test_path_rejects_traversal_and_garbage(bad_path: str) -> None:
    with pytest.raises(SkillValidationError):
        validate_skill_file_path(bad_path)


def test_path_allows_dotted_segment_starting_with_alphanumeric() -> None:
    # 'trailing.' has a dot but starts with alphanumeric — the
    # path validator allows it (filesystem allows files like
    # 'README.', and there's no traversal risk). The dot-segment
    # defense only fires on segments that are *purely* dots.
    validate_skill_file_path("trailing.")
    validate_skill_file_path("name.with.dots.md")


# ---------------------------------------------------------------------------
# Frontmatter splitter — happy path
# ---------------------------------------------------------------------------


_GOOD_DESC = "When the user asks for a code review of the staged diff."


def test_splitter_parses_inline_frontmatter() -> None:
    skill_md = (
        "---\n"
        f"name: code-review\n"
        f"description: {_GOOD_DESC}\n"
        "argument-hint: '[file or PR]'\n"
        "---\n"
        "# Workflow\n"
        "Steps go here.\n"
    )
    common, backend, body = parse_inbound_skill_md(
        skill_md, expected_skill_name="code-review"
    )
    assert common == {"name": "code-review", "description": _GOOD_DESC}
    assert backend == {"claude": {"argument-hint": "[file or PR]"}}
    assert body.startswith("# Workflow")


def test_splitter_falls_back_to_overrides_without_inline_block() -> None:
    body_only = "# Workflow\nNo frontmatter at the top.\n"
    common, backend, parsed_body = parse_inbound_skill_md(
        body_only,
        frontmatter_common_override={"description": _GOOD_DESC},
        expected_skill_name="echo-skill",
    )
    assert common["name"] == "echo-skill"
    assert common["description"] == _GOOD_DESC
    assert backend == {}
    # Body is the unmodified text since there is no frontmatter to strip.
    assert parsed_body == body_only


def test_splitter_overrides_win_over_inline() -> None:
    inline_desc = "Inline description that meets the minimum length easily."
    skill_md = (
        "---\n"
        "name: x\n"
        f"description: {inline_desc}\n"
        "---\nbody"
    )
    override_desc = "Override description that is also long enough to pass."
    common, _, _ = parse_inbound_skill_md(
        skill_md,
        frontmatter_common_override={"description": override_desc},
        expected_skill_name="x",
    )
    assert common["description"] == override_desc


# ---------------------------------------------------------------------------
# Frontmatter splitter — failure modes
# ---------------------------------------------------------------------------


def test_splitter_rejects_unknown_frontmatter_key() -> None:
    skill_md = (
        "---\n"
        "name: x\n"
        f"description: {_GOOD_DESC}\n"
        "unknown-knob: surprise\n"
        "---\nbody"
    )
    with pytest.raises(SkillValidationError, match="unknown frontmatter key"):
        parse_inbound_skill_md(skill_md, expected_skill_name="x")


def test_splitter_rejects_unknown_backend_in_override() -> None:
    skill_md = "---\nname: x\ndescription: " + _GOOD_DESC + "\n---\nbody"
    with pytest.raises(SkillValidationError, match="unknown backend"):
        parse_inbound_skill_md(
            skill_md,
            frontmatter_backend_override={"crystalball": {"argument-hint": "[x]"}},
            expected_skill_name="x",
        )


def test_splitter_rejects_misplaced_field_in_override() -> None:
    """argument-hint is Claude-only; placing it under 'pi' should
    fail rather than silently project to Claude.
    """
    skill_md = "---\nname: x\ndescription: " + _GOOD_DESC + "\n---\nbody"
    with pytest.raises(SkillValidationError, match="not in the pi allowlist"):
        parse_inbound_skill_md(
            skill_md,
            frontmatter_backend_override={"pi": {"argument-hint": "[x]"}},
            expected_skill_name="x",
        )


def test_splitter_rejects_name_mismatch() -> None:
    skill_md = (
        "---\n"
        "name: code-review\n"
        f"description: {_GOOD_DESC}\n"
        "---\nbody"
    )
    with pytest.raises(SkillValidationError, match="does not match"):
        parse_inbound_skill_md(skill_md, expected_skill_name="bug-triage")


def test_splitter_rejects_missing_description() -> None:
    skill_md = "---\nname: x\n---\nbody"
    with pytest.raises(SkillValidationError, match="description"):
        parse_inbound_skill_md(skill_md, expected_skill_name="x")


def test_splitter_rejects_short_description() -> None:
    skill_md = "---\nname: x\ndescription: too short\n---\nbody"
    with pytest.raises(SkillValidationError, match="too short"):
        parse_inbound_skill_md(skill_md, expected_skill_name="x")


def test_splitter_rejects_long_description() -> None:
    long_desc = "x" * (DESCRIPTION_MAX_LENGTH + 1)
    skill_md = (
        "---\n"
        "name: x\n"
        f"description: {long_desc}\n"
        "---\nbody"
    )
    with pytest.raises(SkillValidationError, match="too long"):
        parse_inbound_skill_md(skill_md, expected_skill_name="x")


def test_splitter_minimum_description_boundary() -> None:
    """Exactly DESCRIPTION_MIN_LENGTH characters should pass (inclusive bound)."""
    desc = "x" * DESCRIPTION_MIN_LENGTH
    skill_md = f"---\nname: x\ndescription: {desc}\n---\nbody"
    common, _, _ = parse_inbound_skill_md(skill_md, expected_skill_name="x")
    assert common["description"] == desc


def test_splitter_rejects_invalid_yaml() -> None:
    skill_md = "---\nname: x\ndescription: [unclosed\n---\nbody"
    with pytest.raises(SkillValidationError, match="not valid YAML"):
        parse_inbound_skill_md(skill_md, expected_skill_name="x")


# ---------------------------------------------------------------------------
# Backend frontmatter merging
# ---------------------------------------------------------------------------


def test_merge_drops_other_backend_keys() -> None:
    common = {"name": "x", "description": _GOOD_DESC}
    backend = {
        "claude": {"argument-hint": "[file]"},
        "pi": {"disable_model_invocation": True},
    }
    claude_merged = merge_frontmatter_for_backend(common, backend, "claude")
    assert claude_merged == {
        "name": "x",
        "description": _GOOD_DESC,
        "argument-hint": "[file]",
    }
    assert "disable_model_invocation" not in claude_merged

    pi_merged = merge_frontmatter_for_backend(common, backend, "pi")
    assert pi_merged == {
        "name": "x",
        "description": _GOOD_DESC,
        "disable_model_invocation": True,
    }
    assert "argument-hint" not in pi_merged


def test_merge_treats_claude_code_alias() -> None:
    """``claude-code`` (the BACKEND_CONFIGS legacy alias) projects to
    Claude-side files and should pull from frontmatter_backend.claude.
    """
    common = {"name": "x", "description": _GOOD_DESC}
    backend = {"claude": {"model": "claude-haiku-4-5"}}
    merged = merge_frontmatter_for_backend(common, backend, "claude-code")
    assert merged["model"] == "claude-haiku-4-5"


def test_merge_rejects_unknown_target_backend() -> None:
    with pytest.raises(SkillValidationError):
        merge_frontmatter_for_backend({}, {}, "crystalball")


# ---------------------------------------------------------------------------
# SKILL.md serialization round-trip
# ---------------------------------------------------------------------------


def test_serialize_round_trip_preserves_content() -> None:
    skill_md_in = (
        "---\n"
        "name: round-trip\n"
        f"description: {_GOOD_DESC}\n"
        "---\n"
        "# Body content\n\nMultiple paragraphs.\n"
    )
    common, backend, body = parse_inbound_skill_md(
        skill_md_in, expected_skill_name="round-trip"
    )
    merged = merge_frontmatter_for_backend(common, backend, "claude")
    skill_md_out = serialize_skill_md(merged, body)

    # Re-parse; the result should match the original frontmatter
    # routed back into common/backend, with body unchanged.
    common2, _, body2 = parse_inbound_skill_md(
        skill_md_out, expected_skill_name="round-trip"
    )
    assert common2 == common
    assert body2 == body
