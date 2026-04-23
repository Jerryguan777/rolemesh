"""Exception types for the Safety Framework.

Kept in a dedicated module so both orchestrator and container can raise
the same class without dragging in REST/DB imports. These translate to
HTTP 400 at the admin API boundary.
"""

from __future__ import annotations


class SafetyConfigError(ValueError):
    """Raised when a Rule or Check config is structurally invalid.

    Examples: unknown ``check_id``, stage outside ``check.stages``,
    malformed ``config`` payload. Caught by the REST layer and mapped to
    a 400 response; inside the pipeline a SafetyConfigError is treated
    as a permanent rule-level failure (skip + log, never fail the turn).
    """


class UnknownCheckError(KeyError):
    """Raised by CheckRegistry.get when no check with the given id is registered."""


__all__ = ["SafetyConfigError", "UnknownCheckError"]
