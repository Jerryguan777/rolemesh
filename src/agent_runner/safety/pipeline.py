"""Container-side safety pipeline — re-exports the shared core.

The implementation lives in ``rolemesh.safety.pipeline_core`` so the
orchestrator-side MODEL_OUTPUT handler can execute the same rule
filtering / priority / fail-mode logic without duplicating it. This
shim keeps ``agent_runner.safety.pipeline`` importable for backward
compat of tests and existing call sites.
"""

from __future__ import annotations

from rolemesh.safety.pipeline_core import (
    AUDIT_SUBJECT_TEMPLATE,
    AsyncAuditPublisher,
    AuditPublisher,
    pipeline_run,
)

__all__ = [
    "AUDIT_SUBJECT_TEMPLATE",
    "AsyncAuditPublisher",
    "AuditPublisher",
    "pipeline_run",
]
