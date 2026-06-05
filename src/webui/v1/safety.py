"""``/api/v1/safety/*`` read surface (design §3 Phase 4).

Six GET endpoints — the v1 surface is intentionally read-only:
rule writes (create/update/delete) stay on
``/api/admin/safety/rules`` because rule mutation is an admin-
privileged operation. See ``docs/webui-backend-v1.1-design.md``
§3 Phase 4 for the locked decision.

The lone exception is ``POST /rules:validate`` — a side-effect-free
dry-run (no DB write, no webhook, no audit) that previews whether a
rule body would be accepted on save and surfaces cross-rule warnings.
It does not mutate state, so it does not breach the read-only contract.

Every handler is a thin shim over the shared
:mod:`rolemesh.db.safety` helpers — the same helpers the admin
endpoints call. Centralising query logic there avoids the double-
implementation pitfall the 04 session prompt called out. Each
helper opens a ``tenant_conn(user.tenant_id)`` session so RLS
enforces tenant scope at the DB level; the explicit
``WHERE tenant_id = $1`` inside the SQL is INV-1's second layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg
from fastapi import APIRouter, Depends, Query, Response

from rolemesh.db import (
    count_safety_decisions,
    get_coworker,
    get_safety_decision,
    get_safety_rule,
    list_safety_decisions,
    list_safety_rules,
    list_safety_rules_audit,
    list_visible_platform_rules,
)
from rolemesh.safety.registry import get_orchestrator_registry
from webui.dependencies import get_current_user
from webui.safety_validation import (
    collect_rule_errors,
    compute_info,
    compute_warnings,
)

# Runtime import (not TYPE_CHECKING): FastAPI evaluates the request-body
# annotation at startup to build the validator.
from webui.schemas import SafetyRuleCreate  # noqa: TC001
from webui.schemas_v1 import (
    SafetyCheck,
    SafetyDecision,
    SafetyDecisionPage,
    SafetyFinding,
    SafetyRule,
    SafetyRuleAuditEntry,
    SafetyRuleValidationError,
    SafetyRuleValidationInfo,
    SafetyRuleValidationResult,
    SafetyRuleValidationWarning,
    SafetyStage,
    SafetyVerdictAction,
)
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# Sentinel the SPA sends to query tenant-wide rules (``coworker_id IS
# NULL``) — a real coworker_id is always a UUID, so this can never
# collide with one. See the P0-3 "create-time duplicate detection" flow:
# the rule editor narrows by (check_id, coworker_id, stage) and uses this
# value when the scope is "all coworkers".
_COWORKER_ID_NULL = "__null__"


@router.get("/rules", response_model=list[SafetyRule])
async def list_rules(
    coworker_id: str | None = None,
    check_id: str | None = None,
    stage: SafetyStage | None = None,
    enabled: bool | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SafetyRule]:
    """List safety rules for the caller's tenant.

    All filters combine with AND. Omitting every filter returns the full
    set (the prior behavior). ``coworker_id`` is matched exactly, except
    the ``__null__`` sentinel which filters to tenant-wide rules
    (``coworker_id IS NULL``). ``check_id`` is the P0-3 addition that,
    together with ``coworker_id`` + ``stage``, lets the rule editor
    detect a same-surface duplicate before the user saves.

    Returns the tenant's own rules PLUS the platform-owned rules that
    apply across all tenants. Platform rules are read-only
    (``editable=False``, ``source="platform"``) and only the visible
    tiers (default / transparent_floor) are surfaced — floor-tier rules
    enforce but are never shown. They honor the ``check_id`` / ``stage``
    / ``enabled`` filters. Platform rules are appended only when the
    caller does not narrow by ``coworker_id`` at all: any ``coworker_id``
    filter — a specific id OR ``__null__`` — is an exact-match narrowing
    on tenant rules, and platform rules are not bound to any single
    coworker scope.

    Tenant-rule filters mirror the admin endpoint exactly. Ordering
    (``priority DESC, updated_at DESC``) lives in the helper; platform
    rules are appended after, so the list groups tenant rules first.
    """
    is_null = coworker_id == _COWORKER_ID_NULL
    rows = await list_safety_rules(
        user.tenant_id,
        coworker_id=None if is_null else coworker_id,
        coworker_id_is_null=is_null,
        check_id=check_id,
        stage=stage,
        enabled=enabled,
    )
    out = [_rule_to_response(r) for r in rows]

    if coworker_id is None:
        for prow in await list_visible_platform_rules(user.tenant_id):
            if check_id is not None and prow["check_id"] != check_id:
                continue
            if stage is not None and prow["stage"] != stage:
                continue
            if enabled is not None and bool(prow["enabled"]) != enabled:
                continue
            out.append(_platform_rule_to_response(prow))
    return out


@router.post("/rules:validate", response_model=SafetyRuleValidationResult)
async def validate_rule(
    body: SafetyRuleCreate,
    response: Response,
    rule_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SafetyRuleValidationResult:
    """Dry-run a rule create/update — validate + warn, never write.

    The body is the same ``SafetyRuleCreate`` the admin write path takes,
    and validation runs the identical
    :func:`webui.safety_validation.collect_rule_errors` the write path
    uses — so a body that validates here is guaranteed to be accepted on
    save and vice-versa (the P0-2 invariant). No side effects: no DB
    write, no webhook, no audit.

    Returns ``200`` + ``{valid: true, warnings, info}`` when acceptable,
    or ``422`` + ``{valid: false, errors}`` when not. Pass
    ``?rule_id=sr-5`` on a PATCH dry-run so the duplicate-scope warning
    excludes the rule being edited rather than flagging it against
    itself.
    """
    config = dict(body.config)
    errors: list[SafetyRuleValidationError] = []

    # An unknown / cross-tenant coworker_id surfaces as a field error
    # rather than the admin path's 404, because the dry-run always
    # answers with the {valid, errors} envelope. The lookup is tenant-
    # scoped, so a foreign id simply reads as "not found"; a malformed
    # uuid is treated the same way instead of bubbling a 500.
    if body.coworker_id is not None:
        try:
            cw = await get_coworker(body.coworker_id, tenant_id=user.tenant_id)
        except asyncpg.DataError:
            cw = None
        if cw is None:
            errors.append(
                SafetyRuleValidationError(
                    type="coworker_not_found",
                    loc=["body", "coworker_id"],
                    msg=f"Unknown coworker_id: {body.coworker_id}",
                )
            )

    errors.extend(
        SafetyRuleValidationError(**e)
        for e in await collect_rule_errors(
            body.check_id,
            body.stage,
            config,
            tenant_id=user.tenant_id,
            coworker_id=body.coworker_id,
        )
    )

    valid = not errors
    warnings: list[SafetyRuleValidationWarning] = []
    info: SafetyRuleValidationInfo | None = None
    if valid:
        warnings = [
            SafetyRuleValidationWarning(**w)
            for w in await compute_warnings(
                body.check_id,
                body.stage,
                config,
                tenant_id=user.tenant_id,
                coworker_id=body.coworker_id,
                exclude_rule_id=rule_id,
            )
        ]
        raw_info = compute_info(body.check_id, body.stage, config)
        if raw_info is not None:
            info = SafetyRuleValidationInfo(**raw_info)
    else:
        # Semantically-invalid rule → 422 with the {valid:false,...}
        # body (distinct from FastAPI's own 422 for a malformed request
        # body, which never reaches this handler).
        response.status_code = 422

    return SafetyRuleValidationResult(
        valid=valid, errors=errors, warnings=warnings, info=info
    )


@router.get("/rules/{rule_id}", response_model=SafetyRule)
async def get_rule(
    rule_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
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
    response_model=list[SafetyRuleAuditEntry],
)
async def list_rule_audit(
    rule_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SafetyRuleAuditEntry]:
    """Change-history timeline for one rule, newest first.

    Probes the parent rule via :func:`_get_rule_or_404` before
    reading audit rows — without this guard, a cross-tenant rule_id
    would return an empty 200 (because the audit table is RLS-
    scoped) which is itself a weak signal of "wrong tenant".
    """
    await _get_rule_or_404(rule_id, tenant_id=user.tenant_id)
    rows = await list_safety_rules_audit(
        tenant_id=user.tenant_id,
        rule_id=rule_id,
        limit=limit,
    )
    return [_audit_row_to_response(r) for r in rows]


@router.get("/checks", response_model=list[SafetyCheck])
async def list_checks(
    _user: AuthenticatedUser = Depends(get_current_user),
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


@router.get("/decisions", response_model=SafetyDecisionPage)
async def list_decisions(
    verdict_action: SafetyVerdictAction | None = None,
    coworker_id: str | None = None,
    stage: SafetyStage | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SafetyDecisionPage:
    """Paginated decisions list with a total-count envelope.

    Two parallel DB calls (count + page) so a misbehaving client
    that asks for offset=100k pays for the count once rather than
    once per page. Filter args mirror the admin shape verbatim.
    """
    total = await count_safety_decisions(
        user.tenant_id,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    items = await list_safety_decisions(
        user.tenant_id,
        verdict_action=verdict_action,
        coworker_id=coworker_id,
        stage=stage,
        from_ts=from_ts,
        to_ts=to_ts,
        limit=limit,
        offset=offset,
    )
    return SafetyDecisionPage(
        total=total,
        items=[_decision_row_to_response(r) for r in items],
    )


@router.get("/decisions/{decision_id}", response_model=SafetyDecision)
async def get_decision(
    decision_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
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
