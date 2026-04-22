"""Container-side CheckRegistry wrapper.

Re-exports ``CheckRegistry`` and ``build_container_registry`` from the
orchestrator package — a separate symbol from ``build_orchestrator_
registry`` so a slow check the orchestrator registers cannot leak into
the container image as an unresolvable import. V2 will swap this
builder for one that additionally registers ``RemoteCheck`` proxies
pointing at the orchestrator's slow-check RPC endpoint.
"""

from __future__ import annotations

from rolemesh.safety.registry import CheckRegistry, build_container_registry

__all__ = ["CheckRegistry", "build_container_registry"]
