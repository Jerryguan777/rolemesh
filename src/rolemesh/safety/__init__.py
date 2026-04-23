"""RoleMesh Safety Framework — orchestrator side.

Public surface intentionally narrow: tests and admin APIs import
specific submodules. This package re-exports only the types callers use
in type signatures (REST schemas, DB CRUD, tests).
"""

from .audit import AuditEvent, AuditSink, DbAuditSink
from .engine import SafetyEngine
from .errors import SafetyConfigError, UnknownCheckError
from .loader import (
    fetch_safety_rule_snapshots,
    load_safety_rules_snapshot,
    maybe_register_safety_handler,
)
from .pipeline_core import (
    AUDIT_SUBJECT_TEMPLATE,
    AsyncAuditPublisher,
    AuditPublisher,
    pipeline_run,
)
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
    "AUDIT_SUBJECT_TEMPLATE",
    "CONTROL_STAGES",
    "Action",
    "AsyncAuditPublisher",
    "AuditEvent",
    "AuditPublisher",
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
    "fetch_safety_rule_snapshots",
    "get_orchestrator_registry",
    "load_safety_rules_snapshot",
    "maybe_register_safety_handler",
    "pipeline_run",
]
