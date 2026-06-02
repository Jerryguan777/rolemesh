"""INV-5 Python-half pin: the canonical skill manifest filename is a
constant, not a scattered literal.

If anyone changes ``SKILL_MANIFEST_NAME`` away from ``"SKILL.md"``, this
suite catches it before drift reaches the DB CHECK or the TS frontend.

Anti-mirror discipline:
- We deliberately do NOT walk the same producers that the source uses
  ("does the source say SKILL.md? yes."). Instead we audit the *use
  sites* — every call we know stores or reads the manifest must route
  through the constant. A new caller importing the string literal is
  exactly the regression we want to fail on.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rolemesh.core.skills import SKILL_MANIFEST_NAME, SKILL_MD_FILENAME
from rolemesh.core.skills_consts_pin import (
    SKILL_FILE_PATH_RE,
)
from rolemesh.core.skills_consts_pin import (
    SKILL_MANIFEST_NAME as PIN_NAME,
)


def test_manifest_name_value_is_skill_md() -> None:
    assert SKILL_MANIFEST_NAME == "SKILL.md"


def test_pin_module_reexports_same_object() -> None:
    # ``is`` not ``==``: re-export, not a separate copy. If they ever
    # diverge (e.g. someone hard-codes the value in the pin module
    # instead of re-exporting) the two literals could drift silently.
    assert PIN_NAME is SKILL_MANIFEST_NAME


def test_deprecated_alias_points_at_canonical_constant() -> None:
    # Alias retained for one PR cycle; must not become a second source
    # of truth. Identity check, not equality.
    assert SKILL_MD_FILENAME is SKILL_MANIFEST_NAME


def test_path_regex_rejects_traversal_and_accepts_normal_paths() -> None:
    # Cross-checking the regex constant against its own contract — not
    # against the source-of-truth implementation. These are values we
    # are *asserting* about the contract; an implementation drift in
    # the regex breaks one of these directly.
    assert SKILL_FILE_PATH_RE.match("README.md")
    assert SKILL_FILE_PATH_RE.match("subdir/example.py")
    assert not SKILL_FILE_PATH_RE.match("../escape.md")
    assert not SKILL_FILE_PATH_RE.match("/abs/path.md")
    assert not SKILL_FILE_PATH_RE.match("")


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


_AUDITED_FILES = [
    _PROJECT_ROOT / "src/rolemesh/db/skill.py",
    _PROJECT_ROOT / "src/rolemesh/container/skill_projection.py",
    _PROJECT_ROOT / "src/webui/admin.py",
]


_DOCSTRING_HOLDERS = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings (module / class / func)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_HOLDERS):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _executable_string_literals(source: str) -> list[str]:
    """All str Constant literals in the module *excluding* docstrings."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
    ]


@pytest.mark.parametrize("path", _AUDITED_FILES, ids=lambda p: str(p.relative_to(_PROJECT_ROOT)))
def test_audited_files_have_no_skill_md_string_literal(path: Path) -> None:
    """The known producer/consumer modules must reference the manifest
    via the constant, not the bare string.

    Docstrings are AST constants too, so to keep this honest we audit
    only the *executable* literals; the dedicated module docstring on
    each file is fine because it is the module's first statement, not
    code.
    """
    source = path.read_text()
    literals = _executable_string_literals(source)
    offenders = [lit for lit in literals if lit == "SKILL.md"]
    assert offenders == [], (
        f"{path.relative_to(_PROJECT_ROOT)} contains bare 'SKILL.md' "
        f"literal(s); use SKILL_MANIFEST_NAME from rolemesh.core.skills "
        f"or the lightweight rolemesh.core.skills_consts_pin re-export."
    )


def test_constant_value_matches_db_check_regex_intent() -> None:
    # The DB CHECK enforces the same filename string; if a future
    # author renames the file to e.g. ``manifest.md`` they must rewrite
    # the migration. Pin the value so the migration gets re-graded.
    # (Anti-mirror: not derived from the source — value asserted.)
    assert SKILL_MANIFEST_NAME.endswith(".md")
    assert "/" not in SKILL_MANIFEST_NAME
    assert SKILL_MANIFEST_NAME.upper() == "SKILL.MD"
