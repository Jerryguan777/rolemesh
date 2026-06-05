"""Single source of truth for safety-rule validation + dry-run feedback.

Both the admin write path (``POST``/``PATCH /api/admin/safety/rules``)
and the v1 dry-run (``POST /api/v1/safety/rules:validate``) call
:func:`collect_rule_errors`, so they can never disagree on whether a
rule body is acceptable — that is the P0-2 invariant ("validate passed
but save failed" must be impossible). The admin path wraps a non-empty
result back into the historical ``HTTPException(400)`` string shape; the
dry-run returns the structured list verbatim.

:func:`compute_warnings` and :func:`compute_info` are the dry-run's
incremental value — cross-rule signals (duplicate scope, platform
overlap) and derived metadata the frontend can't compute on its own.
Neither has any side effect: no writes, no webhook, no audit.
"""

from __future__ import annotations

from typing import Any

# Structured error item — the keys mirror a FastAPI/Pydantic error so
# the frontend's existing error parser can consume it unchanged.
#   {"type": str, "loc": list[str], "msg": str}
ErrorItem = dict[str, Any]

# An override may only downgrade to one of these. ``redact`` is refused
# because an override cannot synthesize a modified_payload.
_OVERRIDE_WHITELIST = ("block", "warn", "require_approval")


async def collect_rule_errors(
    check_id: str,
    stage: str,
    config: dict[str, Any],
    *,
    tenant_id: str,
    coworker_id: str | None,
) -> list[ErrorItem]:
    """Return the structured validation errors for a rule body.

    Empty list ⇔ the rule is acceptable. The check order mirrors the
    historical admin fail-fast sequence so the admin wrapper's first
    error (and therefore its 400 ``detail`` substring) is unchanged:
    unknown check → unknown/unsupported stage → config shape → Pydantic
    config model → action_override whitelist → slow-check budget.

    Pydantic surfaces every config error at once (so a body with two bad
    fields yields two items); the business-rule checks are appended
    after. Only the slow-check budget guard touches the DB (coworker MCP
    bindings) and only when the check is ``slow`` at ``PRE_TOOL_CALL``.
    """
    from pydantic import ValidationError

    from rolemesh import db
    from rolemesh.safety.registry import get_orchestrator_registry
    from rolemesh.safety.tool_reversibility import get_tool_reversibility
    from rolemesh.safety.types import Stage

    errors: list[ErrorItem] = []

    registry = get_orchestrator_registry()
    if not registry.has(check_id):
        # Without the check we can't validate stage/config — stop here.
        errors.append(
            {
                "type": "unknown_check_id",
                "loc": ["body", "check_id"],
                "msg": f"Unknown safety check_id: {check_id}",
            }
        )
        return errors
    check = registry.get(check_id)

    stage_enum: Stage | None
    try:
        stage_enum = Stage(stage)
    except ValueError:
        stage_enum = None
        errors.append(
            {
                "type": "unknown_stage",
                "loc": ["body", "stage"],
                "msg": f"Unknown stage: {stage}",
            }
        )
    if stage_enum is not None and stage_enum not in check.stages:
        errors.append(
            {
                "type": "stage_not_supported",
                "loc": ["body", "stage"],
                "msg": (
                    f"Check {check_id} does not support stage {stage}; "
                    f"valid stages: {sorted(s.value for s in check.stages)}"
                ),
            }
        )

    if not isinstance(config, dict):
        errors.append(
            {
                "type": "config_not_object",
                "loc": ["body", "config"],
                "msg": "config must be a JSON object",
            }
        )
        return errors

    # Pydantic validation (unknown keys, wrong types, enum constraints)
    # — the check's declared config_model is the source of truth. Older
    # checks without a model are tolerated, matching the permissive
    # run-time contract. Each sub-error becomes its own structured item
    # with a ``["body", "config", ...]`` loc the frontend maps to a field.
    config_model = getattr(check, "config_model", None)
    if config_model is not None:
        try:
            config_model.model_validate(config)
        except ValidationError as exc:
            for e in exc.errors():
                errors.append(
                    {
                        "type": e["type"],
                        "loc": ["body", "config", *(str(p) for p in e["loc"])],
                        "msg": e["msg"],
                    }
                )

    override = config.get("action_override")
    if override is not None and override not in _OVERRIDE_WHITELIST:
        errors.append(
            {
                "type": "invalid_action_override",
                "loc": ["body", "config", "action_override"],
                "msg": (
                    f"Invalid action_override {override!r}; "
                    f"must be one of {sorted(_OVERRIDE_WHITELIST)} "
                    f"(redact cannot be synthesized via override)"
                ),
            }
        )

    # Slow-check budget guard: a slow check at PRE_TOOL_CALL can't meet
    # the 100ms budget reversible tools require. Expand the rule scope to
    # the affected coworkers' MCP bindings and reject if any reversible
    # tool is in range. coworker_id None ⇒ tenant-wide ⇒ union of every
    # coworker; a set coworker_id ⇒ that one (tenant-scoped lookup, so a
    # cross-tenant id simply yields no coworkers and no error here).
    if (
        stage_enum is not None
        and getattr(check, "cost_class", "cheap") == "slow"
        and stage_enum == Stage.PRE_TOOL_CALL
    ):
        scope_coworkers = []
        if coworker_id is not None:
            cw = await db.get_coworker(coworker_id, tenant_id=tenant_id)
            if cw is not None:
                scope_coworkers.append(cw)
        else:
            scope_coworkers.extend(
                await db.get_coworkers_for_tenant(tenant_id)
            )
        for cw_any in scope_coworkers:
            tools = await db.list_coworker_mcp_configs(
                cw_any.id, tenant_id=tenant_id
            )
            for mcp in tools:
                overrides = dict(mcp.tool_reversibility or {})
                for bare_name in overrides:
                    if get_tool_reversibility(bare_name, overrides):
                        errors.append(
                            {
                                "type": "slow_check_budget",
                                "loc": ["body", "check_id"],
                                "msg": (
                                    f"Rule with slow check {check_id!r} at "
                                    f"PRE_TOOL_CALL is blocked: coworker "
                                    f"{cw_any.name!r} configures reversible "
                                    f"tool {bare_name!r} which exceeds the "
                                    "100 ms budget. Narrow the rule scope "
                                    "or use a different stage."
                                ),
                            }
                        )
                        # One offending tool is enough — matches the
                        # historical fail-fast behavior.
                        return errors
    return errors


async def compute_warnings(
    check_id: str,
    stage: str,
    config: dict[str, Any],
    *,
    tenant_id: str,
    coworker_id: str | None,
    exclude_rule_id: str | None = None,
) -> list[dict[str, Any]]:
    """Non-blocking cross-rule signals for the dry-run.

    ``duplicate_scope`` flags an existing tenant rule on the exact same
    ``(check_id, coworker_id, stage)`` surface; ``platform_overlap``
    flags a read-only platform rule for the same check. ``exclude_rule_id``
    lets a PATCH dry-run ignore the rule being edited so it doesn't flag
    itself. First-version scope per the P0-2 spec — more warning types
    are additive.
    """
    from rolemesh import db

    warnings: list[dict[str, Any]] = []

    existing = await db.list_safety_rules(
        tenant_id,
        coworker_id=coworker_id,
        coworker_id_is_null=coworker_id is None,
        check_id=check_id,
        stage=stage,
    )
    dup_ids = [r.id for r in existing if r.id != exclude_rule_id]
    if dup_ids:
        scope = coworker_id if coworker_id is not None else "all coworkers"
        warnings.append(
            {
                "type": "duplicate_scope",
                "message": (
                    f"A {check_id} rule already exists for the same scope "
                    f"(coworker_id={scope}) at {stage} "
                    f"(rule {', '.join(dup_ids)}). Saving this rule will "
                    "result in two rules covering the same surface — they "
                    "may conflict."
                ),
                "related_rule_ids": dup_ids,
                "severity": "medium",
            }
        )

    platform = await db.list_visible_platform_rules(tenant_id)
    plat_ids = [str(p["id"]) for p in platform if p["check_id"] == check_id]
    if plat_ids:
        warnings.append(
            {
                "type": "platform_overlap",
                "message": (
                    f"A platform-tier {check_id} rule "
                    f"({', '.join(plat_ids)}) also applies to this tenant. "
                    "The platform rule is the strict outer bound and cannot "
                    "be edited here."
                ),
                "related_rule_ids": plat_ids,
                "severity": "low",
            }
        )

    return warnings


def compute_info(
    check_id: str, stage: str, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Derived metadata the frontend can't compute on its own.

    ``action_resolution`` is the effective action (a valid
    ``action_override`` wins, else the check's natural action for the
    stage); ``stage_supported_actions`` is the action set this
    ``(check, stage)`` accepts. Returns ``None`` when check/stage can't
    be resolved (the body is invalid anyway). ``effective_for_coworkers``
    is intentionally deferred — it needs a coworker join and isn't worth
    the cost for the first version.
    """
    from rolemesh.safety.registry import get_orchestrator_registry
    from rolemesh.safety.types import Stage

    registry = get_orchestrator_registry()
    if not registry.has(check_id):
        return None
    check = registry.get(check_id)
    try:
        stage_enum = Stage(stage)
    except ValueError:
        return None

    supported = sorted(check.supported_actions.get(stage_enum, frozenset()))
    natural = check.natural_actions.get(stage_enum)
    override = config.get("action_override") if isinstance(config, dict) else None
    action_resolution = (
        override if override in _OVERRIDE_WHITELIST else natural
    )
    return {
        "action_resolution": action_resolution,
        "stage_supported_actions": supported,
    }


__all__ = ["collect_rule_errors", "compute_info", "compute_warnings"]
