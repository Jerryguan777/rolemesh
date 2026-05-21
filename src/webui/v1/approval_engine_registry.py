"""Process-wide ApprovalEngine handle for ``/api/v1/*`` consumers.

The WebUI bootstrap (``webui.main.lifespan``) constructs **one**
:class:`rolemesh.approval.engine.ApprovalEngine` for the process
and registers it here. Both the legacy ``/api/admin/*`` decide
endpoint and the new ``/api/v1/approvals/{id}/decide`` endpoint
resolve through this registry so they hit the same state machine
without re-instantiating the engine.

Registering ``None`` is the "approval feature is off" signal; the
v1 decide endpoint returns 503 with code
``APPROVAL_ENGINE_UNAVAILABLE`` instead of silently no-op'ing.

Keeping the handle here (not in :mod:`webui.admin`) avoids a circular
dependency: ``webui.v1.*`` cannot import :mod:`webui.admin` without
dragging in the legacy admin schemas, but :mod:`webui.admin` happily
imports this module for its own ``set_approval_engine``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rolemesh.approval.engine import ApprovalEngine

__all__ = ["get_approval_engine", "set_approval_engine"]

_engine: ApprovalEngine | None = None


def set_approval_engine(engine: ApprovalEngine | None) -> None:
    """Install (or clear) the process-wide ApprovalEngine."""
    global _engine
    _engine = engine


def get_approval_engine() -> ApprovalEngine | None:
    """Return the registered engine, or None if approvals are off."""
    return _engine
