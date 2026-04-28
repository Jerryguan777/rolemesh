"""Tests for ``pi.coding_agent.core.sdk._assemble_system_prompt``.

Two behaviours under test:

* Skills XML block is included in the assembled system prompt only
  when the agent has the ``read`` tool available. The block tells
  the model "Use the read tool to load a skill's file..." — telling
  it about skills it can't actually read is worse than silence.
* All resource_loader pieces (``get_system_prompt`` +
  ``get_append_system_prompt``) appear in the output regardless of
  read-tool availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pi.coding_agent.core.sdk import _assemble_system_prompt


@dataclass
class _FakeSkill:
    name: str
    description: str
    file_path: str = "/fake/SKILL.md"
    disable_model_invocation: bool = False


class _FakeResourceLoader:
    def __init__(
        self,
        system_prompt: str | None = None,
        append: list[str] | None = None,
        skills: list[_FakeSkill] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._append = list(append or [])
        self._skills = {s.name: s for s in (skills or [])}

    def get_system_prompt(self) -> str | None:
        return self._system_prompt

    def get_append_system_prompt(self) -> list[str]:
        return list(self._append)

    def get_skills(self) -> dict[str, Any]:
        return dict(self._skills)


_DESC = "When the user types XYZ, do the demo workflow."


def test_skills_block_present_when_read_tool_available() -> None:
    rl = _FakeResourceLoader(
        skills=[_FakeSkill(name="demo", description=_DESC)]
    )
    out = _assemble_system_prompt(rl, has_read_tool=True)
    assert "<available_skills>" in out
    assert "<name>demo</name>" in out
    assert _DESC in out


def test_skills_block_omitted_when_read_tool_missing() -> None:
    rl = _FakeResourceLoader(
        skills=[_FakeSkill(name="demo", description=_DESC)]
    )
    out = _assemble_system_prompt(rl, has_read_tool=False)
    assert "<available_skills>" not in out
    # The skill description must NOT leak into the prompt either —
    # the model would otherwise see a stranded reference.
    assert _DESC not in out


def test_other_pieces_survive_no_read_tool() -> None:
    """``get_system_prompt`` + ``get_append_system_prompt`` are
    independent of skills and must always be assembled.
    """
    rl = _FakeResourceLoader(
        system_prompt="ROLE: helpful assistant",
        append=["EXTRA: company policy"],
        skills=[_FakeSkill(name="demo", description=_DESC)],
    )
    out = _assemble_system_prompt(rl, has_read_tool=False)
    assert "ROLE: helpful assistant" in out
    assert "EXTRA: company policy" in out
    assert "<available_skills>" not in out


def test_disable_model_invocation_skill_filtered_by_pi_format() -> None:
    """``format_skills_for_prompt`` filters out
    ``disable_model_invocation=True``. Sanity check that the
    assembler doesn't override that filter.
    """
    rl = _FakeResourceLoader(
        skills=[
            _FakeSkill(name="hidden", description=_DESC, disable_model_invocation=True),
            _FakeSkill(name="visible", description=_DESC),
        ]
    )
    out = _assemble_system_prompt(rl, has_read_tool=True)
    assert "<name>visible</name>" in out
    assert "<name>hidden</name>" not in out


def test_empty_loader_returns_empty_string() -> None:
    rl = _FakeResourceLoader()
    assert _assemble_system_prompt(rl, has_read_tool=True) == ""


def test_none_loader_returns_empty_string() -> None:
    """Defensive path for the (unusual) caller that passes None."""
    assert _assemble_system_prompt(None) == ""
