"""Container-side Safety Framework (runs inside agent_runner).

Exports the hook handler + pipeline entry point + registry builder.
Types and check classes re-export from ``rolemesh.safety`` to keep a
single source of truth; see ``agent_runner/safety/types.py``.
"""

from .hook_handler import SafetyHookHandler
from .pipeline import pipeline_run
from .registry import CheckRegistry, build_default_registry

__all__ = [
    "CheckRegistry",
    "SafetyHookHandler",
    "build_default_registry",
    "pipeline_run",
]
