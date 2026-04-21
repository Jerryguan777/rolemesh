"""Built-in safety checks bundled with RoleMesh.

V1 ships ``pii.regex`` only. V2 adds orchestrator-only slow checks
(``presidio.pii``, ``llm_guard.prompt_injection``, etc.) here as well.
"""

from .pii_regex import PIICode, PIIRegexCheck

__all__ = ["PIICode", "PIIRegexCheck"]
