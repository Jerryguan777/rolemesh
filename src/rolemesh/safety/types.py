"""Core type definitions for the Safety Framework.

The design holds four primitives: ``Stage`` (where in an agent turn a
check runs), ``SafetyContext`` (read-only input to a check),
``SafetyCheck`` (the detection unit, a Protocol), and ``Rule`` (a
DB-backed configuration row that binds a check + config to a tenant/
coworker/stage).

``Verdict`` is the return type from every check and carries zero or
more ``Finding`` records for the audit trail.

Stability contract: the string value of every ``Stage`` /
``Severity`` / ``Action`` is part of the on-wire NATS protocol and the
persisted DB schema, so renames require a schema migration and must
bump ``SafetyCheck.version``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping


class Stage(StrEnum):
    """Hook point where a check executes.

    Control stages (fail-close on handler exceptions):
      - INPUT_PROMPT
      - PRE_TOOL_CALL
      - MODEL_OUTPUT
      - EGRESS_REQUEST   — EC-3; evaluated by the egress gateway on
                           every outbound TCP/DNS attempt.

    Observational stages (fail-safe on handler exceptions):
      - POST_TOOL_RESULT
      - PRE_COMPACTION

    V1 only wires up PRE_TOOL_CALL; the other values are declared so
    check classes can advertise multi-stage support without breakage
    once V2 extends the hook registry.
    """

    INPUT_PROMPT = "input_prompt"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_RESULT = "post_tool_result"
    MODEL_OUTPUT = "model_output"
    PRE_COMPACTION = "pre_compaction"
    EGRESS_REQUEST = "egress_request"


# Stages whose handler exceptions must fail-close (propagate). Pipeline
# and hook bridge both consult this set. EGRESS_REQUEST is listed even
# though the container pipeline never sees it — the enum wins-over-
# convention rule means a future refactor that accidentally feeds an
# EGRESS_REQUEST context to the container pipeline will fail-close
# rather than fail-open.
CONTROL_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.INPUT_PROMPT,
        Stage.PRE_TOOL_CALL,
        Stage.MODEL_OUTPUT,
        Stage.EGRESS_REQUEST,
    }
)


Severity = Literal["info", "low", "medium", "high", "critical"]
Action = Literal["allow", "block", "redact", "warn", "require_approval"]
CostClass = Literal["cheap", "slow"]


class SafetyObservabilityCode(StrEnum):
    """Pipeline-level observability codes emitted on fail-open paths.

    These are **not** check-specific codes — they're published when a
    check couldn't run to completion (RPC timeout, HTTP transport
    error, missing config). Dashboards that aggregate by
    ``Finding.code`` pivot across the whole safety system should
    filter on ``SAFETY.*`` to see outages independently of
    detection-code counts.

    Convention: every code here starts with ``SAFETY.`` so operators
    can ``WHERE code LIKE 'SAFETY.%'`` to isolate infrastructure
    events vs detection events. Individual check ``supported_codes``
    sets MUST NOT include ``SAFETY.*`` values — keeping the
    namespace separate is the whole point.
    """

    RPC_TIMEOUT = "SAFETY.RPC_TIMEOUT"
    RPC_ERROR = "SAFETY.RPC_ERROR"
    CONFIG_ERROR = "SAFETY.CONFIG_ERROR"
    TRANSPORT_ERROR = "SAFETY.TRANSPORT_ERROR"
    HTTP_ERROR = "SAFETY.HTTP_ERROR"
    PARSE_ERROR = "SAFETY.PARSE_ERROR"


@dataclass(frozen=True)
class ToolInfo:
    """Tool metadata visible to PRE_TOOL_CALL / POST_TOOL_RESULT checks.

    ``reversible`` is authoritative for the V2 cost_class x reversibility
    matrix. V1 does not enforce the matrix but already surfaces the flag
    so new checks can consult it without a follow-up refactor.
    """

    name: str
    reversible: bool = False


@dataclass(frozen=True)
class SafetyContext:
    """Read-only payload a check receives.

    ``payload`` shape varies by stage:

      - INPUT_PROMPT:     {"prompt": str}
      - PRE_TOOL_CALL:    {"tool_name": str, "tool_input": dict}
      - POST_TOOL_RESULT: {"tool_name": str, "tool_input": dict,
                           "tool_result": str, "is_error": bool}
      - MODEL_OUTPUT:     {"text": str}
      - PRE_COMPACTION:   {"transcript_path": str | None,
                           "messages": list}
    """

    stage: Stage
    tenant_id: str
    coworker_id: str
    user_id: str
    job_id: str
    conversation_id: str
    # Mapping (read-only protocol) rather than dict so checks cannot
    # mutate a shared payload between rules in the same turn. Pipeline
    # constructs a fresh SafetyContext with a new payload for redact
    # chaining.
    payload: Mapping[str, Any]
    tool: ToolInfo | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """Detection record for the audit log.

    ``code`` is a check-defined stable enum string (e.g. ``PII.SSN``).
    Adapter checks wrapping a third-party library MUST map the library's
    internal types to a closed set of stable codes and drop anything
    outside that set — see §7.1 in the Safety Framework design doc.
    """

    code: str
    severity: Severity
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """Check output.

    ``action="redact"`` requires ``modified_payload`` to be the stage-
    appropriate payload shape with the offending content rewritten.
    ``action="warn"`` uses ``appended_context`` to inject a string into
    the agent's follow-up context (hook bridge wires this up at V2).
    ``action="require_approval"`` is a V2 concept and ignored by V1
    pipeline (which treats it as block); V2 will rewrite to an approval
    request event.
    """

    action: Action = "allow"
    reason: str | None = None
    modified_payload: Any | None = None
    findings: list[Finding] = field(default_factory=list)
    appended_context: str | None = None


@dataclass(frozen=True)
class Rule:
    """A single configuration row.

    ``coworker_id=None`` means tenant-wide scope. ``priority`` is
    descending (higher runs first). V2 will add ``active_hours`` and
    ``active_days`` for time-window scheduling.

    Frozen so callers use ``dataclasses.replace`` for updates; this
    makes "rule snapshot taken at container start is immutable until
    next run" a type-system guarantee rather than a convention.
    """

    id: str
    tenant_id: str
    coworker_id: str | None
    stage: Stage
    check_id: str
    config: Mapping[str, Any]
    priority: int = 100
    enabled: bool = True
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Serialize to the plain-dict shape the container pipeline consumes.

        Plain dict because AgentInitData is JSON-serialized — no
        dataclass dependency inside the container snapshot.
        """
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "coworker_id": self.coworker_id,
            "stage": self.stage.value,
            "check_id": self.check_id,
            "config": dict(self.config),
            "priority": self.priority,
            "enabled": self.enabled,
            "description": self.description,
        }


@runtime_checkable
class SafetyCheck(Protocol):
    """Every detection unit satisfies this Protocol.

    ``id`` is globally unique (e.g. ``pii.regex``,
    ``llm_guard.prompt_injection``). ``version`` is bumped on
    non-backward-compatible code changes. ``supported_codes`` publishes
    the stable Finding.code strings a check may emit — tests enforce
    that nothing outside this set leaks out.

    ``config_model`` is optional; when set, the REST layer uses it to
    pydantic-validate ``config`` payloads on rule create/update. A
    missing model means the check tolerates arbitrary dicts (legacy
    behaviour) — new checks are expected to provide one so that typos
    like ``{"SSN": "yes"}`` fail loud at REST time rather than acting
    subtly wrong at run-time.
    """

    id: str
    version: str
    stages: frozenset[Stage]
    cost_class: CostClass
    supported_codes: frozenset[str]
    # Type is Any to keep this Protocol importable without pydantic at
    # module load. Concrete checks declare it as type[BaseModel] | None.
    config_model: Any

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict: ...


__all__ = [
    "CONTROL_STAGES",
    "Action",
    "CostClass",
    "Finding",
    "Rule",
    "SafetyCheck",
    "SafetyContext",
    "SafetyObservabilityCode",
    "Severity",
    "Stage",
    "ToolInfo",
    "Verdict",
]
