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

# How a check decides the action it returns on a hit. This is metadata
# for the rule-editor UI — the pipeline does not consume it. The three
# values map onto three genuinely different check architectures, and
# the UI renders a different editor experience for each:
#
#   "fixed"         The action is hardcoded in check(): a hit always
#                   returns the same action (today: always "block").
#                   natural_actions[stage] is that action. UI shows
#                   "This check defaults to: BLOCK".
#
#   "config_routed" The action is chosen per-finding by the rule's
#                   config (e.g. presidio.pii block_codes vs
#                   redact_codes). With the default/empty config a hit
#                   is inert (returns "allow"), so natural_actions is
#                   "allow" — UI shows "configure per-category below;
#                   inert until configured" rather than a default badge.
#
#   "aggregated"    The check only votes; a separate layer decides the
#                   effective verdict (egress.domain_rule returns
#                   "allow" on a match; the gateway aggregator blocks
#                   when no rule allowed). natural_actions is the
#                   check's own return ("allow"); UI explains the
#                   effective action is decided by aggregation.
ActionModel = Literal["fixed", "config_routed", "aggregated"]


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
    ``action="require_approval"`` is a verdict that blocks the turn —
    the pipeline short-circuits on it exactly like ``block``. It is
    kept as a distinct action for audit/reporting purposes.
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

    Action metadata (``action_model`` / ``natural_actions`` /
    ``supported_actions``) is **descriptive, not prescriptive**: it
    reports what the check's current code does, and the pipeline does
    not consume it. It exists so the rule-editor UI can show the
    default action and grey out actions that cannot be carried out for
    a given (check, stage). The cardinal rule is that these fields
    describe the check — they must never drive a change to ``check()``
    behaviour. Invariants (enforced by tests in
    ``tests/safety/test_action_matrix.py``):

      1. ``natural_actions.keys() == supported_actions.keys() ==
         stages`` — every supported stage is declared once in each map.
      2. ``natural_actions[stage] in supported_actions[stage]`` — the
         default is always one of the offered actions.
      3. Running ``check()`` on an input that fires, with a config that
         enables detection but does NOT override the action, returns
         ``natural_actions[ctx.stage]``. For ``config_routed`` /
         ``aggregated`` checks that "firing under no action routing"
         outcome is ``allow`` (the check is inert / only votes). The
         matrix is anchored to real runtime behaviour, so drift fails
         the test.

    ``natural_actions`` is the action a hit produces under the default
    (empty) config — see ``ActionModel`` for what "default" means for
    each architecture. ``supported_actions`` is the set of actions a
    rule on that (check, stage) can meaningfully produce, gated by real
    capabilities: ``redact`` only where the check can emit a
    ``modified_payload``; ``warn`` only where a stage consumes
    ``appended_context``; ``require_approval`` only where the stage has
    an approval surface; ``block`` / ``allow`` everywhere.
    """

    id: str
    version: str
    stages: frozenset[Stage]
    cost_class: CostClass
    supported_codes: frozenset[str]
    action_model: ActionModel
    natural_actions: Mapping[Stage, Action]
    supported_actions: Mapping[Stage, frozenset[Action]]
    # Type is Any to keep this Protocol importable without pydantic at
    # module load. Concrete checks declare it as type[BaseModel] | None.
    config_model: Any

    async def check(
        self, ctx: SafetyContext, config: dict[str, Any]
    ) -> Verdict: ...


__all__ = [
    "CONTROL_STAGES",
    "Action",
    "ActionModel",
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
