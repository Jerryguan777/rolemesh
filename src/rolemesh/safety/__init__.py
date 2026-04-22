"""RoleMesh Safety Framework — orchestrator side.

Public surface intentionally narrow: tests and admin APIs import
specific submodules. This package re-exports only the types callers use
in type signatures (REST schemas, DB CRUD, tests).
"""

from .audit import AuditEvent, AuditSink, DbAuditSink
from .engine import SafetyEngine
from .errors import SafetyConfigError, UnknownCheckError
from .loader import load_safety_rules_snapshot, maybe_register_safety_handler
from .registry import (
    CheckRegistry,
    build_container_registry,
    build_orchestrator_registry,
    get_orchestrator_registry,
)
from .subscriber import SafetyEventsSubscriber
from .types import (
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
    "AuditEvent",
    "AuditSink",
    "CheckRegistry",
    "CostClass",
    "DbAuditSink",
    "Finding",
    "Rule",
    "SafetyCheck",
    "SafetyConfigError",
    "SafetyContext",
    "SafetyEngine",
    "SafetyEventsSubscriber",
    "Severity",
    "Stage",
    "ToolInfo",
    "UnknownCheckError",
    "Verdict",
    "build_container_registry",
    "build_orchestrator_registry",
    "get_orchestrator_registry",
    "load_safety_rules_snapshot",
    "maybe_register_safety_handler",
]
