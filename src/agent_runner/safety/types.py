"""Container-side mirror of rolemesh.safety.types.

Re-exports the orchestrator-side types so handlers inside the container
can ``from agent_runner.safety.types import Stage, SafetyContext, ...``
without reaching across packages in their own code. The container
deliberately does not introduce a second set of dataclasses — keeping a
single source of truth prevents schema drift between the two sides.

Rules themselves travel through AgentInitData as plain dicts (see
``rolemesh.safety.types.Rule.to_snapshot_dict``); the pipeline consumes
those dicts directly without reconstituting the Rule dataclass, so no
dataclass is required on the container-side wire.
"""

from __future__ import annotations

from rolemesh.safety.types import (
    CONTROL_STAGES,
    Action,
    CostClass,
    Finding,
    Rule,
    SafetyCheck,
    SafetyContext,
    Severity,
    Stage,
    ToolInfo,
    Verdict,
)

__all__ = [
    "CONTROL_STAGES",
    "Action",
    "CostClass",
    "Finding",
    "Rule",
    "SafetyCheck",
    "SafetyContext",
    "Severity",
    "Stage",
    "ToolInfo",
    "Verdict",
]
