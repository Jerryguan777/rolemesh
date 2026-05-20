"""Tiny re-export shim for skill-related constants.

Other modules that only need the filename or path regex can import
from here without pulling in the heavier ``core.skills`` module
(which transitively imports ``yaml`` and the full frontmatter
parser). Keeping the dependency surface minimal makes these
constants safe to reference from low-level layers such as IPC
protocol or container plumbing.

Pinned by ``tests/test_skill_manifest_constant.py``: the string
value here is part of the storage contract and must never drift
from the DB CHECK constraint or the TS frontend's matching
constant.
"""

from __future__ import annotations

from rolemesh.core.skills import SKILL_FILE_PATH_RE, SKILL_MANIFEST_NAME

__all__ = ["SKILL_MANIFEST_NAME", "SKILL_FILE_PATH_RE"]
