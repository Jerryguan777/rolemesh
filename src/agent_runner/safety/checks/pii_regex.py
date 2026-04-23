"""Container-side re-export of the PII regex check.

``PIIRegexCheck`` is identical object-identity with the orchestrator
class — a test in ``tests/safety/test_pii_regex.py`` pins that
property so the two import paths cannot drift.
"""

from __future__ import annotations

from rolemesh.safety.checks.pii_regex import PIICode, PIIRegexCheck

__all__ = ["PIICode", "PIIRegexCheck"]
