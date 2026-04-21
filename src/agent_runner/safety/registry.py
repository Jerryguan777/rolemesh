"""Container-side CheckRegistry wrapper.

Re-exports ``CheckRegistry`` and ``build_default_registry`` from the
orchestrator package. Kept as a separate module so V2 can swap
``build_default_registry`` for a variant that additionally registers
``RemoteCheck`` proxies pointing at the orchestrator's slow-check RPC
endpoint — without touching the orchestrator's own ``build_default_
registry``.
"""

from __future__ import annotations

from rolemesh.safety.registry import CheckRegistry, build_default_registry

__all__ = ["CheckRegistry", "build_default_registry"]
