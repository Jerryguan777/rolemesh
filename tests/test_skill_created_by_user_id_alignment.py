"""Anti-regression: ``Skill`` audit-field name is ``created_by_user_id``.

00b renamed the DB column ``skills.created_by`` → ``created_by_user_id``;
03b PR 2 lifted that rename through the Python dataclass, Pydantic
response model, and admin handler.

This suite pins the post-rename world. Anti-mirror: we assert the
*absence* of the legacy ``created_by`` field name in the audited
surfaces, not the presence of the new name in the same surface that
produced it.

Out of scope:
* ``eval_runs.created_by`` — a different field on a different entity.
  Guarded by the include-filter on the audited paths so a future
  caller in that table doesn't fail this check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rolemesh.core.types import Skill
from webui.schemas import SkillResponse

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_skill_dataclass_field_name_is_created_by_user_id() -> None:
    """The dataclass field must be ``created_by_user_id``. Equality
    test (not isinstance) because the rename is the load-bearing
    invariant here — a renamed field still typed correctly is the
    failure we want to catch.
    """
    field_names = {f for f in Skill.__dataclass_fields__}
    assert "created_by_user_id" in field_names, (
        "Skill dataclass missing created_by_user_id — the rename "
        "from 'created_by' regressed."
    )
    assert "created_by" not in field_names, (
        "Skill dataclass still has legacy 'created_by' field; the "
        "v1.1 03b rename was supposed to drop it."
    )


def test_skill_response_pydantic_field_name_is_created_by_user_id() -> None:
    """Admin REST response schema mirrors the dataclass."""
    schema_fields = set(SkillResponse.model_fields)
    assert "created_by_user_id" in schema_fields, (
        "SkillResponse missing created_by_user_id"
    )
    assert "created_by" not in schema_fields, (
        "SkillResponse still has legacy 'created_by'"
    )


_LEGACY_SKILL_CREATED_BY = re.compile(
    # ``Skill.created_by`` or ``skill.created_by`` or ``s.created_by`` —
    # qualified attribute access on a Skill-shaped object. The
    # negative-lookahead rejects the new name (``created_by_user_id``)
    # and the uuid variable (``created_by_uuid``).
    r"\b(?:Skill|skill|s)\.created_by(?!_user_id|_uuid)\b"
)

# Audited modules: every site that historically read Skill.created_by.
# Adding a new caller imports the legacy attribute and trips this
# guard. ``eval_runs`` / ``EvalRun`` is intentionally untouched —
# different entity, retained name.
_AUDITED_FILES = [
    _PROJECT_ROOT / "src/rolemesh/db/skill.py",
    _PROJECT_ROOT / "src/rolemesh/core/types.py",
    _PROJECT_ROOT / "src/webui/admin.py",
    _PROJECT_ROOT / "src/webui/schemas.py",
]


@pytest.mark.parametrize(
    "path", _AUDITED_FILES, ids=lambda p: str(p.relative_to(_PROJECT_ROOT)),
)
def test_no_legacy_skill_created_by_attribute_in_audited_files(
    path: Path,
) -> None:
    source = path.read_text()
    offenders = [
        (lineno, line)
        for lineno, line in enumerate(source.splitlines(), 1)
        if _LEGACY_SKILL_CREATED_BY.search(line)
    ]
    assert offenders == [], (
        f"{path.relative_to(_PROJECT_ROOT)} references legacy "
        f"Skill.created_by — should be created_by_user_id:\n"
        + "\n".join(f"  L{n}: {ln}" for n, ln in offenders)
    )


def test_eval_runs_created_by_is_not_affected() -> None:
    """Negative control: ``eval_runs.created_by`` is a separate entity
    and must keep its original column name. If a refactor accidentally
    lifted both, this test surfaces it as a separate failure.
    """
    store = (_PROJECT_ROOT / "src/rolemesh/evaluation/store.py").read_text()
    # ``created_by`` (no suffix) must still appear — it is the
    # column name on the eval_runs table.
    assert re.search(r"\bcreated_by\b", store), (
        "eval_runs.created_by appears to have been renamed too — "
        "PR 2 was supposed to be skills-only."
    )
