"""JSON codec for SafetyContext / Verdict round-trip over NATS.

The container-side ``RemoteCheck`` and the orchestrator-side
``SafetyRpcServer`` both need to serialize and deserialize the same
objects. Keeping the field mapping in one module is the single source
of truth — a silent drift (e.g. dropping ``metadata`` on the wire)
would manifest as "my slow check can't see the user_id in production
but works in tests".

Field-by-field serialization rather than dataclasses.asdict() because:

  - ``Stage`` must be emitted as its ``.value`` string, not an enum
    instance (which would become a non-JSONable dict under asdict).
  - We want to explicitly coerce ``payload`` / ``metadata`` from
    ``Mapping`` to plain ``dict`` so ``json.dumps`` sees real dicts.
  - ``modified_payload`` is ``Any``; we pass it through without
    narrowing so a check that returns a non-dict still serializes.

Stability contract: these dict shapes are part of the wire format.
Renaming a key requires bumping a version marker and supporting both
keys for a deprecation window. For now there is no version marker
because V2 is a greenfield rollout; add one at the first breaking
change.
"""

from __future__ import annotations

from typing import Any

from .types import Finding, SafetyContext, Stage, ToolInfo, Verdict


def serialize_context(ctx: SafetyContext) -> dict[str, Any]:
    return {
        "stage": ctx.stage.value,
        "tenant_id": ctx.tenant_id,
        "coworker_id": ctx.coworker_id,
        "user_id": ctx.user_id,
        "job_id": ctx.job_id,
        "conversation_id": ctx.conversation_id,
        "payload": dict(ctx.payload),
        "tool": (
            {"name": ctx.tool.name, "reversible": ctx.tool.reversible}
            if ctx.tool is not None
            else None
        ),
        "metadata": dict(ctx.metadata),
    }


def deserialize_context(data: dict[str, Any]) -> SafetyContext:
    tool_data = data.get("tool")
    tool: ToolInfo | None = None
    if isinstance(tool_data, dict):
        tool = ToolInfo(
            name=str(tool_data.get("name", "")),
            reversible=bool(tool_data.get("reversible", False)),
        )
    return SafetyContext(
        stage=Stage(str(data["stage"])),
        tenant_id=str(data["tenant_id"]),
        coworker_id=str(data["coworker_id"]),
        user_id=str(data["user_id"]),
        job_id=str(data["job_id"]),
        conversation_id=str(data["conversation_id"]),
        payload=data.get("payload") or {},
        tool=tool,
        metadata=data.get("metadata") or {},
    )


def serialize_verdict(v: Verdict) -> dict[str, Any]:
    return {
        "action": v.action,
        "reason": v.reason,
        "modified_payload": v.modified_payload,
        "findings": [
            {
                "code": f.code,
                "severity": f.severity,
                "message": f.message,
                "metadata": dict(f.metadata),
            }
            for f in v.findings
        ],
        "appended_context": v.appended_context,
    }


def deserialize_verdict(data: dict[str, Any]) -> Verdict:
    findings_raw = data.get("findings") or []
    findings: list[Finding] = []
    for f in findings_raw:
        if not isinstance(f, dict):
            continue
        findings.append(
            Finding(
                code=str(f.get("code", "")),
                # pydantic-style Severity literal — we don't validate
                # here because the wire already came from a trusted
                # orchestrator registry. A bogus value would surface
                # downstream when the audit sink tries to persist it.
                severity=f.get("severity", "info"),
                message=str(f.get("message", "")),
                metadata=dict(f.get("metadata") or {}),
            )
        )
    # Action is typed as a Literal; we do not validate at the codec
    # layer because the pipeline's _V2_ALLOWED_ACTIONS check surfaces
    # an unknown value as a fail-close error on control stages.
    return Verdict(
        action=data.get("action", "allow"),
        reason=data.get("reason"),
        modified_payload=data.get("modified_payload"),
        findings=findings,
        appended_context=data.get("appended_context"),
    )


__all__ = [
    "deserialize_context",
    "deserialize_verdict",
    "serialize_context",
    "serialize_verdict",
]
