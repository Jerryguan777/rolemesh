"""INV-5 (Python ↔ TS): ``SKILL_MANIFEST_NAME`` must match across
the two language sides.

00b PR1 pinned the Python side; 03b PR 3 lifts the same constant
into the TS frontend. A future rename — even one as innocent as
``manifest.md`` — has to update both files or this test fails.

The DB CHECK 'SKILL.md' literal was deliberately *not* added (per
the 03b prompt's Open Question 2 resolution): the app layer plus
this two-way lint is enough, and a DB CHECK would lock the manifest
filename behind a migration step.
"""

from __future__ import annotations

import re
from pathlib import Path

from rolemesh.core.skills_consts_pin import SKILL_MANIFEST_NAME

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TS_CONSTANT_FILE = (
    _PROJECT_ROOT / "web" / "src" / "api" / "skill_constants.ts"
)


def _extract_ts_constant() -> str:
    """Read the TS module text and return the literal value of
    ``SKILL_MANIFEST_NAME``.

    Anti-mirror discipline: we deliberately do not import the file
    (it's TypeScript, not Python anyway) — we re-parse the literal
    so a refactor that swapped declaration style stays caught.
    """
    text = _TS_CONSTANT_FILE.read_text()
    # ``export const SKILL_MANIFEST_NAME = "SKILL.md";``
    match = re.search(
        r"""export\s+const\s+SKILL_MANIFEST_NAME\s*=\s*"([^"]+)"\s*;""",
        text,
    )
    assert match is not None, (
        f"Could not parse SKILL_MANIFEST_NAME from {_TS_CONSTANT_FILE}. "
        f"File contents:\n{text}"
    )
    return match.group(1)


def test_ts_constant_file_exists() -> None:
    assert _TS_CONSTANT_FILE.exists(), (
        f"TS skill constants file missing at {_TS_CONSTANT_FILE} — "
        f"INV-5 needs both sides present."
    )


def test_python_and_ts_skill_manifest_names_agree() -> None:
    ts_value = _extract_ts_constant()
    assert ts_value == SKILL_MANIFEST_NAME, (
        f"INV-5 drift: TS SKILL_MANIFEST_NAME={ts_value!r} but "
        f"Python SKILL_MANIFEST_NAME={SKILL_MANIFEST_NAME!r}. Update "
        f"both sides in lockstep."
    )


def test_python_constant_is_skill_md_value() -> None:
    # Anti-mirror sanity: the constant pin against the actual string.
    # A future "rename to manifest.md" PR must change this assertion
    # as part of its diff — surfaced here instead of buried in test
    # fixtures.
    assert SKILL_MANIFEST_NAME == "SKILL.md"
