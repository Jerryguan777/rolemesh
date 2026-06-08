"""``/api/v1/safety/*`` surface (design §3 Phase 4 + admin migration).

Read endpoints (rules / checks / decisions / audit) plus the rule
**write** surface (create / update / delete) and the streaming CSV
export — all migrated off the legacy ``/api/admin/safety/*`` face.
Reads gate on ``safety.read``; writes gate on ``safety.rule.manage``
(read/write split: an admin manages tenant safety, a member with only
``safety.read`` cannot mutate). Cross-tenant rows/coworkers read as
404, never 403.

Every handler is a thin shim over the shared
:mod:`rolemesh.db.safety` helpers — the same helpers the legacy admin
endpoints called. Centralising query logic there avoids the double-
implementation pitfall the 04 session prompt called out. Each
helper opens a ``tenant_conn(user.tenant_id)`` session so RLS
enforces tenant scope at the DB level; the explicit
``WHERE tenant_id = $1`` inside the SQL is INV-1's second layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import asyncpg
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse

from rolemesh import db
from rolemesh.auth.bootstrap_actor import resolve_actor_user_id
from rolemesh.db import (
    count_safety_decisions,
    count_safety_rules,
    count_safety_rules_audit,
    create_safety_rule,
    delete_safety_rule,
    get_safety_decision,
    get_safety_rule,
    list_safety_decisions,
    list_safety_rules,
    list_safety_rules_audit,
    list_visible_platform_rules,
    update_safety_rule,
)
from rolemesh.safety.registry import get_orchestrator_registry
from webui.dependencies import require_action

# Request bodies — kept at runtime (NOT type-checking-only): FastAPI resolves
# these annotations at request time via get_type_hints under
# ``from __future__ import annotations``, so the names must be in module globals.
from webui.schemas import SafetyRuleCreate, SafetyRuleUpdate  # noqa: TC001
from webui.schemas_v1 import (
    SafetyCheck,
    SafetyDecision,
    SafetyDecisionPage,
    SafetyFinding,
    SafetyRule,
    SafetyRuleAuditEntry,
    SafetyRuleAuditPage,
    SafetyRulePage,
    SafetyStage,
    SafetyVerdictAction,
)
from webui.v1._pagination import DEFAULT_PAGE_LIMIT, LimitParam, OffsetParam
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from rolemesh.auth.provider import AuthenticatedUser
    from rolemesh.core.types import Coworker
    from rolemesh.safety.types import Rule as SafetyRuleDataclass

router = APIRouter(prefix="/safety", tags=["Safety"])


# ---------------------------------------------------------------------------
# Wire projections
# ---------------------------------------------------------------------------


def _rule_to_response(r: SafetyRuleDataclass) -> SafetyRule:
    """Project the safety ``Rule`` dataclass onto the wire shape.

    ``r.stage`` is a ``StrEnum`` whose ``.value`` is the wire-side
    snake_case label — Pydantic's ``SafetyStage`` Literal is the
    closed enum on the API surface. The two never drift because the
    snake_case labels are anchored in
    :class:`rolemesh.safety.types.Stage` and the OpenAPI yaml's
    ``SafetyStage`` enum is hand-paired with the Pydantic Literal.
    """
    return SafetyRule(
        id=r.id,
        tenant_id=r.tenant_id,
        coworker_id=r.coworker_id,
        stage=r.stage.value,  # type: ignore[arg-type]
        check_id=r.check_id,
        config=r.config,
        priority=r.priority,
        enabled=r.enabled,
        description=r.description,
        created_at=r.created_at,
        updated_at=r.updated_at,
        source="tenant",
        tier=None,
        editable=True,
    )


def _platform_rule_to_response(row: dict[str, Any]) -> SafetyRule:
    """Project a platform rule row onto the wire shape.

    Platform rules are cross-tenant and have no owning tenant, so
    ``tenant_id`` is rendered as an empty string (the "global / platform"
    signal) and ``coworker_id`` is None. They are read-only on every
    tenant surface: ``editable=False`` and ``source="platform"``. ``tier``
    carries which of the visible tiers (default / transparent_floor) this
    rule belongs to.
    """
    config = row["config"]
    return SafetyRule(
        id=str(row["id"]),
        tenant_id="",
        coworker_id=None,
        stage=row["stage"],
        check_id=str(row["check_id"]),
        config=config if isinstance(config, dict) else {},
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        description=str(row["description"]),
        created_at=str(row.get("created_at", "")),
        updated_at=str(row.get("updated_at", "")),
        source="platform",
        tier=row["tier"],
        editable=False,
    )


def _decision_row_to_response(row: dict[str, object]) -> SafetyDecision:
    """Coerce a ``list_safety_decisions`` / ``get_safety_decision``
    row into the wire shape.

    ``findings`` arrives as ``list[dict]``; each entry has the
    ``code``/``severity``/``message`` triple plus optional metadata.
    Using ``row.get`` rather than indexing means one branch handles
    both the list and detail projections.
    """
    raw_findings = row.get("findings") or []
    findings: list[SafetyFinding] = []
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        metadata = f.get("metadata")
        findings.append(
            SafetyFinding(
                code=str(f.get("code", "")),
                severity=f.get("severity", "info"),  # type: ignore[arg-type]
                message=str(f.get("message", "")),
                metadata=metadata if isinstance(metadata, dict) else None,
            )
        )
    return SafetyDecision(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=(
            str(row["coworker_id"]) if row.get("coworker_id") else None
        ),
        conversation_id=(
            str(row["conversation_id"])
            if row.get("conversation_id") is not None
            else None
        ),
        job_id=str(row["job_id"]) if row.get("job_id") is not None else None,
        stage=row["stage"],  # type: ignore[arg-type]
        verdict_action=row["verdict_action"],  # type: ignore[arg-type]
        triggered_rule_ids=[
            str(rid) for rid in (row.get("triggered_rule_ids") or [])
        ],
        findings=findings,
        context_digest=str(row.get("context_digest", "") or ""),
        context_summary=str(row.get("context_summary", "") or ""),
        source=row.get("source", "tenant"),  # type: ignore[arg-type]
        created_at=str(row.get("created_at", "") or ""),
    )


def _audit_row_to_response(row: dict[str, object]) -> SafetyRuleAuditEntry:
    """Project one ``safety_rules_audit`` row onto the wire shape."""
    return SafetyRuleAuditEntry(
        id=str(row["id"]),
        rule_id=str(row["rule_id"]),
        tenant_id=str(row["tenant_id"]),
        action=row["action"],  # type: ignore[arg-type]
        actor_user_id=(
            str(row["actor_user_id"]) if row.get("actor_user_id") else None
        ),
        before_state=row.get("before_state") if isinstance(
            row.get("before_state"), dict
        ) else None,
        after_state=row.get("after_state") if isinstance(
            row.get("after_state"), dict
        ) else None,
        created_at=str(row.get("created_at", "") or ""),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_rule_or_404(
    rule_id: str, *, tenant_id: str
) -> SafetyRuleDataclass:
    """Fetch a rule or raise the design §13 404 envelope.

    ``asyncpg.DataError`` covers the malformed-UUID case — instead
    of bubbling out as a 500, we map it to the same 404 the
    cross-tenant guess gets, so probes for "is this UUID valid"
    return identical responses regardless of input shape.
    """
    try:
        rule = await get_safety_rule(rule_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        rule = None
    if rule is None:
        raise_error_response(
            "NOT_FOUND",
            "Safety rule not found.",
            status_code=404,
            details={"rule_id": rule_id},
        )
    return rule


async def _get_coworker_or_404(coworker_id: str, *, tenant_id: str) -> Coworker:
    """Fetch a coworker or raise the design §13 404 envelope.

    Guards rule creation against attaching to a coworker the caller's
    tenant doesn't own — a cross-tenant ``coworker_id`` reads as 404
    (never 403) so existence isn't leaked.
    """
    try:
        cw = await db.get_coworker(coworker_id, tenant_id=tenant_id)
    except asyncpg.DataError:
        cw = None
    if cw is None:
        raise_error_response(
            "NOT_FOUND",
            "Coworker not found.",
            status_code=404,
            details={"coworker_id": coworker_id},
        )
    return cw


async def _validate_safety_rule_body(
    check_id: str,
    stage: str,
    config: dict[str, object],
    *,
    tenant_id: str,
    coworker_id: str | None,
) -> None:
    """Reject (400 ``INVALID_RULE``) a rule that cannot run at run-time.

    REST is the strict boundary: misconfigured rules are rejected here
    before they land in the DB. The container-side pipeline is permissive
    on stale snapshots (log + skip), but a fresh admin action must fail
    loud so typos surface immediately.

    Migrated verbatim (behaviour-preserving) from the legacy admin
    handler, including the V2 P0.4 reversibility guard: a ``slow`` check
    at ``PRE_TOOL_CALL`` is refused when any in-scope coworker configures
    a reversible tool (the 100 ms budget can't be met), and the V2 P1.1
    ``action_override`` whitelist.
    """
    # Lazy import avoids a WebUI → rolemesh.safety cycle at module load.
    from pydantic import ValidationError

    from rolemesh.safety.tool_reversibility import get_tool_reversibility
    from rolemesh.safety.types import Stage

    registry = get_orchestrator_registry()
    if not registry.has(check_id):
        raise_error_response(
            "INVALID_RULE",
            f"Unknown safety check_id: {check_id}",
            status_code=400,
        )
    check = registry.get(check_id)
    try:
        stage_enum = Stage(stage)
    except ValueError:
        raise_error_response(
            "INVALID_RULE", f"Unknown stage: {stage}", status_code=400,
        )
    if stage_enum not in check.stages:
        raise_error_response(
            "INVALID_RULE",
            (
                f"Check {check_id} does not support stage {stage}; "
                f"valid stages: {sorted(s.value for s in check.stages)}"
            ),
            status_code=400,
        )
    if not isinstance(config, dict):
        raise_error_response(
            "INVALID_RULE", "config must be a JSON object", status_code=400,
        )
    # Pydantic validation (unknown keys, wrong types) — the check's
    # declared config_model is the source of truth. Older checks without
    # a model are tolerated, matching the permissive run-time contract.
    config_model = getattr(check, "config_model", None)
    if config_model is not None:
        try:
            config_model.model_validate(config)
        except ValidationError as exc:
            raise_error_response(
                "INVALID_RULE",
                f"Invalid config for {check_id}: {exc.errors()}",
                status_code=400,
            )

    # V2 P1.1: action_override whitelist. redact is explicitly refused
    # because the check did not produce a modified_payload.
    override = config.get("action_override") if isinstance(config, dict) else None
    if override is not None:
        valid = {"block", "warn", "require_approval"}
        if override not in valid:
            raise_error_response(
                "INVALID_RULE",
                (
                    f"Invalid action_override {override!r}; "
                    f"must be one of {sorted(valid)} "
                    f"(redact cannot be synthesized via override)"
                ),
                status_code=400,
            )

    # V2 P0.4: reversibility guard at admin time. Only runs when the check
    # is slow AND the stage is PRE_TOOL_CALL — other combinations have no
    # budget conflict. coworker_id None → tenant-wide rule → union of every
    # coworker's MCP bindings; set → that single coworker's bindings.
    if (
        getattr(check, "cost_class", "cheap") == "slow"
        and stage_enum == Stage.PRE_TOOL_CALL
    ):
        scope_coworkers: list[Coworker] = []
        if coworker_id is not None:
            cw = await db.get_coworker(coworker_id, tenant_id=tenant_id)
            if cw is not None:
                scope_coworkers.append(cw)
        else:
            scope_coworkers.extend(await db.get_coworkers_for_tenant(tenant_id))
        for cw_any in scope_coworkers:
            tools = await db.list_coworker_mcp_configs(
                cw_any.id, tenant_id=tenant_id,
            )
            for mcp in tools:
                overrides = dict(mcp.tool_reversibility or {})
                for bare_name in overrides:
                    if get_tool_reversibility(bare_name, overrides):
                        raise_error_response(
                            "INVALID_RULE",
                            (
                                f"Rule with slow check {check_id!r} at "
                                f"PRE_TOOL_CALL is blocked: coworker "
                                f"{cw_any.name!r} configures reversible tool "
                                f"{bare_name!r} which exceeds the 100 ms "
                                "budget. Narrow the rule scope or use a "
                                "different stage."
                            ),
                            status_code=400,
                        )


async def _publish_rule_changed(action: str, rule: SafetyRuleDataclass) -> None:
    """Publish a ``safety.rule.changed`` event to the egress gateway.

    Best-effort — a NATS outage here must NOT fail the REST call. The
    caller's DB row is already committed; the gateway recovers on its
    next full snapshot. Import lazily so a webui process without the
    egress extras still works (it just doesn't publish).
    """
    try:
        from rolemesh.egress.orch_glue import publish_rule_changed
        from webui import main as webui_main
    except ImportError:
        return
    nc = getattr(webui_main, "_nc", None)
    if nc is None:
        return
    import contextlib

    with contextlib.suppress(Exception):
        await publish_rule_changed(nc, action=action, rule=rule.to_snapshot_dict())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/rules", response_model=SafetyRulePage)
async def list_rules(
    coworker_id: str | None = None,
    stage: SafetyStage | None = None,
    enabled: bool | None = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
    offset: OffsetParam = 0,
    user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> SafetyRulePage:
    """List safety rules for the caller's tenant (offset/limit paged).

    Returns the tenant's own rules PLUS the platform-owned rules that
    apply across all tenants. Platform rules are read-only
    (``editable=False``, ``source="platform"``) and only the visible
    tiers (default / transparent_floor) are surfaced — floor-tier rules
    enforce but are never shown. They honor the ``stage`` / ``enabled``
    filters. Like tenant-wide (``coworker_id IS NULL``) rules, platform
    rules are excluded when the caller filters by a specific
    ``coworker_id``.

    Only the tenant rows are paginated at the DB. Platform rules are a
    small read-only reference set, so they ride on the FIRST page only
    (``offset == 0``), after the tenant slice; ``total`` counts them so
    the SPA's pagination math stays correct.
    """
    rows = await list_safety_rules(
        user.tenant_id,
        coworker_id=coworker_id,
        stage=stage,
        enabled=enabled,
        limit=limit,
        offset=offset,
    )
    items = [_rule_to_response(r) for r in rows]
    total = await count_safety_rules(
        user.tenant_id,
        coworker_id=coworker_id,
        stage=stage,
        enabled=enabled,
    )

    if coworker_id is None:
        platform = [
            _platform_rule_to_response(prow)
            for prow in await list_visible_platform_rules(user.tenant_id)
            if (stage is None or prow["stage"] == stage)
            and (enabled is None or bool(prow["enabled"]) == enabled)
        ]
        total += len(platform)
        if offset == 0:
            items.extend(platform)

    return SafetyRulePage(
        items=items, total=total, limit=limit, offset=offset,
    )


@router.get("/rules/{rule_id}", response_model=SafetyRule)
async def get_rule(
    rule_id: str,
    user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> SafetyRule:
    """Single rule, scoped to the caller's tenant.

    Resolves a tenant-owned rule first; falls back to a visible platform
    rule with this id so the ids returned by ``GET /rules`` are all
    individually fetchable. Cross-tenant lookups (and floor-tier platform
    ids) return 404 (not 403) so a UUID-guess probe cannot infer a row's
    existence in another tenant.
    """
    try:
        rule = await get_safety_rule(rule_id, tenant_id=user.tenant_id)
    except asyncpg.DataError:
        rule = None
    if rule is not None:
        return _rule_to_response(rule)

    for prow in await list_visible_platform_rules(user.tenant_id):
        if str(prow["id"]) == rule_id:
            return _platform_rule_to_response(prow)

    raise_error_response(
        "NOT_FOUND",
        "Safety rule not found.",
        status_code=404,
        details={"rule_id": rule_id},
    )


@router.get(
    "/rules/{rule_id}/audit",
    response_model=SafetyRuleAuditPage,
)
async def list_rule_audit(
    rule_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    offset: OffsetParam = 0,
    user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> SafetyRuleAuditPage:
    """Change-history timeline for one rule, newest first (offset/limit paged).

    Probes the parent rule via :func:`_get_rule_or_404` before
    reading audit rows — without this guard, a cross-tenant rule_id
    would return an empty 200 (because the audit table is RLS-
    scoped) which is itself a weak signal of "wrong tenant".

    Keeps its own ``limit`` cap (max 500, vs the shared 200) because a
    compliance timeline is occasionally read in larger pulls.
    """
    await _get_rule_or_404(rule_id, tenant_id=user.tenant_id)
    rows = await list_safety_rules_audit(
        tenant_id=user.tenant_id,
        rule_id=rule_id,
        limit=limit,
        offset=offset,
    )
    total = await count_safety_rules_audit(
        tenant_id=user.tenant_id, rule_id=rule_id,
    )
    return SafetyRuleAuditPage(
        items=[_audit_row_to_response(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/checks", response_model=list[SafetyCheck])
async def list_checks(
    _user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> list[SafetyCheck]:
    """Registered check metadata for the rule editor.

    Stable alphabetical ordering on ``check.id`` so dashboards that
    cache the response don't see phantom reorderings. The handler
    matches the admin variant byte-for-byte — both reach the same
    in-process registry singleton.
    """
    checks = sorted(get_orchestrator_registry().all(), key=lambda c: c.id)
    out: list[SafetyCheck] = []
    for c in checks:
        model = c.config_model
        schema: dict[str, object] | None = (
            model.model_json_schema()
            if model is not None and hasattr(model, "model_json_schema")
            else None
        )
        out.append(
            SafetyCheck(
                id=c.id,
                version=c.version,
                stages=sorted(s.value for s in c.stages),  # type: ignore[arg-type]
                cost_class=c.cost_class,  # type: ignore[arg-type]
                supported_codes=sorted(c.supported_codes),
                config_schema=schema,
                action_model=c.action_model,  # type: ignore[arg-type]
                natural_actions={
                    st.value: act  # type: ignore[misc]
                    for st, act in c.natural_actions.items()
                },
                supported_actions={
                    st.value: sorted(acts)  # type: ignore[misc]
                    for st, acts in c.supported_actions.items()
                },
            )
        )
    return out


def _parse_decision_ts(raw: str | None, field: str) -> datetime | None:
    """Parse an ISO-8601 ``from_ts`` / ``to_ts`` query value to a datetime.

    asyncpg binds a timestamptz parameter from a datetime, not a str, so the
    range filter is parsed here at the edge. A malformed value is a 422
    rather than a query-time DataError (which surfaced as an unhandled 500).
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise_error_response(
            "INVALID_REQUEST",
            f"Query parameter '{field}' must be an ISO-8601 timestamp.",
            status_code=422,
        )


@router.get("/decisions", response_model=SafetyDecisionPage)
async def list_decisions(
    verdict_action: SafetyVerdictAction | None = None,
    coworker_id: str | None = None,
    stage: SafetyStage | None = None,
    check_id: str | None = None,
    rule_id: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
    offset: OffsetParam = 0,
    user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> SafetyDecisionPage:
    """Paginated decisions list with a total-count envelope.

    Two parallel DB calls (count + page) so a misbehaving client
    that asks for offset=100k pays for the count once rather than
    once per page. Filter args mirror the admin shape verbatim.

    ``check_id`` and ``rule_id`` narrow to decisions a given check /
    rule triggered. A decision carries no ``check_id`` directly, so the
    helper translates the check into its rule ids (tenant + platform
    catalogs) and tests for array overlap; ``rule_id`` tests array
    containment against ``triggered_rule_ids``.
    """
    from_dt = _parse_decision_ts(from_ts, "from_ts")
    to_dt = _parse_decision_ts(to_ts, "to_ts")
    total = await count_safety_decisions(
        user.tenant_id,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_dt,
        to_ts=to_dt,
        check_id=check_id,
        rule_id=rule_id,
    )
    items = await list_safety_decisions(
        user.tenant_id,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_dt,
        to_ts=to_dt,
        check_id=check_id,
        rule_id=rule_id,
        limit=limit,
        offset=offset,
    )
    return SafetyDecisionPage(
        items=[_decision_row_to_response(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/decisions/{decision_id}", response_model=SafetyDecision)
async def get_decision(
    decision_id: str,
    user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> SafetyDecision:
    """One safety decision detail row.

    Cross-tenant lookups return 404 (not 403). A malformed UUID
    surfaces as 404 too so guess probes return the same shape
    regardless of input validity.
    """
    try:
        row = await get_safety_decision(
            decision_id, tenant_id=user.tenant_id
        )
    except asyncpg.DataError:
        row = None
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "Safety decision not found.",
            status_code=404,
            details={"decision_id": decision_id},
        )
    return _decision_row_to_response(row)


# ---------------------------------------------------------------------------
# Rule writes (safety.rule.manage) — migrated from /api/admin/safety/rules.
# ---------------------------------------------------------------------------


@router.post("/rules", response_model=SafetyRule, status_code=201)
async def create_rule(
    body: SafetyRuleCreate,
    user: AuthenticatedUser = Depends(require_action("safety.rule.manage")),
) -> SafetyRule:
    """Create a tenant safety rule.

    A non-null ``coworker_id`` is validated for tenant ownership first
    (cross-tenant → 404) so the scope-expansion query in validation can't
    leak existence. The rule body is then validated against the check
    registry (and the reversibility guard) before the DB write.
    """
    if body.coworker_id is not None:
        await _get_coworker_or_404(body.coworker_id, tenant_id=user.tenant_id)
    await _validate_safety_rule_body(
        body.check_id,
        body.stage,
        dict(body.config),
        tenant_id=user.tenant_id,
        coworker_id=body.coworker_id,
    )
    actor = await resolve_actor_user_id(user.tenant_id, user.user_id)
    rule = await create_safety_rule(
        tenant_id=user.tenant_id,
        coworker_id=body.coworker_id,
        stage=body.stage,
        check_id=body.check_id,
        config=body.config,
        priority=body.priority,
        enabled=body.enabled,
        description=body.description,
        actor_user_id=actor,
    )
    await _publish_rule_changed("created", rule)
    return _rule_to_response(rule)


@router.patch("/rules/{rule_id}", response_model=SafetyRule)
async def update_rule(
    rule_id: str,
    body: SafetyRuleUpdate,
    user: AuthenticatedUser = Depends(require_action("safety.rule.manage")),
) -> SafetyRule:
    """Update a tenant safety rule.

    Re-validates when ``check_id`` / ``stage`` / ``config`` change so the
    effective triple stays runnable. Platform rules are not reachable here
    (only tenant-owned ids resolve); a cross-tenant / unknown id is 404.
    """
    existing = await _get_rule_or_404(rule_id, tenant_id=user.tenant_id)

    eff_check = body.check_id if body.check_id is not None else existing.check_id
    eff_stage = body.stage if body.stage is not None else existing.stage.value
    eff_config = body.config if body.config is not None else existing.config
    if (
        body.check_id is not None
        or body.stage is not None
        or body.config is not None
    ):
        await _validate_safety_rule_body(
            eff_check,
            eff_stage,
            dict(eff_config),
            tenant_id=user.tenant_id,
            coworker_id=existing.coworker_id,
        )

    actor = await resolve_actor_user_id(user.tenant_id, user.user_id)
    updated = await update_safety_rule(
        rule_id,
        tenant_id=user.tenant_id,
        stage=body.stage,
        check_id=body.check_id,
        config=body.config,
        priority=body.priority,
        enabled=body.enabled,
        description=body.description,
        actor_user_id=actor,
    )
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Safety rule not found.",
            status_code=404,
            details={"rule_id": rule_id},
        )
    await _publish_rule_changed("updated", updated)
    return _rule_to_response(updated)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    user: AuthenticatedUser = Depends(require_action("safety.rule.manage")),
) -> Response:
    """Delete a tenant safety rule (cross-tenant / unknown id → 404)."""
    existing = await _get_rule_or_404(rule_id, tenant_id=user.tenant_id)
    actor = await resolve_actor_user_id(user.tenant_id, user.user_id)
    await delete_safety_rule(
        rule_id, tenant_id=user.tenant_id, actor_user_id=actor,
    )
    await _publish_rule_changed("deleted", existing)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Streaming CSV export of safety decisions (safety.read). Migrated from
# /api/admin/tenants/{tid}/safety/decisions.csv — tenant is derived from the
# authenticated session (no URL tenant id); cross-tenant relies on RLS.
# ---------------------------------------------------------------------------

# Compact, operator-friendly column set. Deliberately NOT the full audit row —
# ``findings`` is flattened to parallel code/severity lists so the CSV is a
# pivot table, not a nested JSON blob per cell.
_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "created_at",
    "tenant_id",
    "coworker_id",
    "conversation_id",
    "job_id",
    "stage",
    "verdict_action",
    "triggered_rule_ids",
    "finding_codes",
    "finding_severities",
    "context_summary",
)

_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def _csv_escape(value: object) -> str:
    """RFC 4180 quoting + CSV-formula-injection guard.

    Wraps comma/quote/newline fields per RFC 4180, and prefixes a leading
    ``=``/``+``/``-``/``@``/tab/CR with ``'`` so Excel/Sheets render the cell
    as literal text rather than a (possibly network-fetching) formula —
    audit data carries agent-influenced text (e.g. ``tool=<name>``).
    """
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in _FORMULA_PREFIXES:
        s = "'" + s
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _format_row(row: dict[str, object]) -> str:
    triggered = cast("list[object]", row.get("triggered_rule_ids") or [])
    findings = cast("list[dict[str, object]]", row.get("findings") or [])
    codes = "|".join(
        str(f.get("code", "")) for f in findings if isinstance(f, dict)
    )
    sevs = "|".join(
        str(f.get("severity", "")) for f in findings if isinstance(f, dict)
    )
    triggered_flat = "|".join(str(t) for t in triggered)
    fields = [
        _csv_escape(row.get("id")),
        _csv_escape(row.get("created_at")),
        _csv_escape(row.get("tenant_id")),
        _csv_escape(row.get("coworker_id")),
        _csv_escape(row.get("conversation_id")),
        _csv_escape(row.get("job_id")),
        _csv_escape(row.get("stage")),
        _csv_escape(row.get("verdict_action")),
        _csv_escape(triggered_flat),
        _csv_escape(codes),
        _csv_escape(sevs),
        _csv_escape(row.get("context_summary")),
    ]
    return ",".join(fields) + "\n"


@router.get("/decisions.csv")
async def export_decisions_csv(
    from_ts: str | None = None,
    to_ts: str | None = None,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    stage: str | None = None,
    user: AuthenticatedUser = Depends(require_action("safety.read")),
) -> StreamingResponse:
    """Stream the caller's tenant safety_decisions as CSV.

    Uses a Postgres cursor so a 100k-row export stays in constant memory;
    the response starts flowing as soon as the first chunk is ready. The
    flat column set is ``_CSV_COLUMNS`` — full JSON for any row is at
    ``GET /safety/decisions/{id}``.
    """
    tid = user.tenant_id
    from_dt = _parse_decision_ts(from_ts, "from_ts")
    to_dt = _parse_decision_ts(to_ts, "to_ts")

    async def _generate() -> AsyncIterator[bytes]:
        yield (",".join(_CSV_COLUMNS) + "\n").encode("utf-8")
        async for chunk in db.stream_safety_decisions(
            tid,
            from_ts=from_dt,
            to_ts=to_dt,
            verdict_action=verdict_action,
            coworker_id=coworker_id,
            stage=stage,
        ):
            for row in chunk:
                yield _format_row(row).encode("utf-8")

    today = datetime.now(UTC).strftime("%Y%m%d")
    filename = f"safety-decisions-{tid}-{today}.csv"
    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
