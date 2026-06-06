"""``/api/v1/platform/safety/rules`` REST surface (platform safety rules).

The platform safety rule catalog (``platform_safety_rules``) is
cross-tenant: every rule enforces on EVERY tenant's agents. This surface
is platform-plane only — every handler is gated on
``safety.platform.manage``, which lives in ``_PLATFORM_ONLY_ACTIONS`` so no
tenant role (not even an owner) can reach it (see
:mod:`rolemesh.auth.permissions`).

Contrast with the tenant-facing ``/api/v1/safety/rules``:

  * It surfaces a tenant's OWN rules plus the *visible* platform tiers
    (default / transparent_floor), read-only. This surface exposes ALL
    tiers including ``floor`` — the platform operator manages floor too;
    only its *visibility* is suppressed for tenants.
  * Writes go through ``admin_conn`` (the business role has no
    INSERT/UPDATE/DELETE on this catalog), via the helpers in
    :mod:`rolemesh.db.platform_safety`.

Seeded factory defaults (``is_seeded = TRUE``) are managed disable-only:
config / enabled edits are allowed, but a hard DELETE is refused (409) —
the next build-time seed would resurrect the row, so disabling is the
sanctioned way to suppress one.

No live reload is published here: platform rules are loaded into a fresh
agent's safety pipeline at spawn (``fetch_platform_rule_snapshots``), so a
change takes effect for newly spawned agents automatically; already-running
agents keep the rules they booted with, matching the existing container-
side rule lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import asyncpg
from fastapi import APIRouter, Depends, Response

from rolemesh.core.logger import get_logger
from rolemesh.db import (
    create_platform_rule,
    delete_platform_rule,
    get_platform_rule,
    list_all_platform_rules,
    set_platform_rule_enabled,
    update_platform_rule,
)
from rolemesh.safety.registry import get_orchestrator_registry
from webui.dependencies import require_action
from webui.schemas_v1 import (
    PlatformSafetyRule,
    PlatformSafetyRuleCreate,
    PlatformSafetyRuleUpdate,
)
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser

logger = get_logger()

router = APIRouter(prefix="/platform/safety/rules", tags=["Platform"])


def _to_response(row: dict[str, Any]) -> PlatformSafetyRule:
    """Project a ``rolemesh.db.platform_safety`` row dict onto the wire shape."""
    config = row.get("config")
    return PlatformSafetyRule(
        id=str(row["id"]),
        tier=row["tier"],
        stage=row["stage"],
        check_id=str(row["check_id"]),
        config=config if isinstance(config, dict) else {},
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        description=str(row["description"]),
        is_seeded=bool(row["is_seeded"]),
        created_at=str(row.get("created_at", "")),
        updated_at=str(row.get("updated_at", "")),
    )


def _validate_rule(check_id: str, stage: str, config: dict[str, object]) -> None:
    """Reject (400 ``INVALID_RULE``) a platform rule that cannot run.

    Same registry-backed validation as tenant rule creation — the check must
    exist, support the stage, and its declared ``config_model`` (if any) must
    accept the config; the ``action_override`` whitelist is enforced too.

    The tenant path's PRE_TOOL_CALL reversibility guard is intentionally NOT
    applied: it is scoped to a single tenant's coworker MCP bindings, which
    has no meaning for a cross-tenant platform rule.
    """
    from pydantic import ValidationError

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
    override = config.get("action_override")
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


async def _get_rule_or_404(rule_id: str) -> dict[str, Any]:
    """Fetch a platform rule or raise the 404 envelope (bad UUID → 404 too)."""
    try:
        row = await get_platform_rule(rule_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "Platform safety rule not found.",
            status_code=404,
            details={"rule_id": rule_id},
        )
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PlatformSafetyRule])
async def list_platform_rules(
    _user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> list[PlatformSafetyRule]:
    """List every platform safety rule across ALL tiers (floor included)."""
    rows = await list_all_platform_rules()
    return [_to_response(r) for r in rows]


@router.post("", response_model=PlatformSafetyRule, status_code=201)
async def create_platform_rule_endpoint(
    body: PlatformSafetyRuleCreate,
    user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> PlatformSafetyRule:
    """Create a platform safety rule (``is_seeded=False``).

    The body is validated against the check registry before the write. A
    duplicate ``(tier, check_id, stage)`` identity returns 409 ``CONFLICT``.
    """
    _validate_rule(body.check_id, body.stage, dict(body.config))
    try:
        row = await create_platform_rule(
            tier=body.tier,
            stage=body.stage,
            check_id=body.check_id,
            config=dict(body.config),
            priority=body.priority,
            description=body.description,
        )
    except asyncpg.UniqueViolationError:
        raise_error_response(
            "CONFLICT",
            (
                f"A platform rule already exists for tier={body.tier}, "
                f"check_id={body.check_id}, stage={body.stage}."
            ),
            status_code=409,
        )
    logger.info(
        "platform safety rule created",
        actor_user_id=user.user_id,
        rule_id=row["id"],
        tier=row["tier"],
        check_id=row["check_id"],
        stage=row["stage"],
    )
    return _to_response(row)


@router.get("/{rule_id}", response_model=PlatformSafetyRule)
async def get_platform_rule_endpoint(
    rule_id: str,
    _user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> PlatformSafetyRule:
    """One platform safety rule by id (any tier). Unknown id → 404."""
    row = await _get_rule_or_404(rule_id)
    return _to_response(row)


@router.patch("/{rule_id}", response_model=PlatformSafetyRule)
async def update_platform_rule_endpoint(
    rule_id: str,
    body: PlatformSafetyRuleUpdate,
    user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> PlatformSafetyRule:
    """Patch a rule's config / priority / description / enabled.

    ``tier`` / ``stage`` / ``check_id`` are immutable. When ``config``
    changes it is re-validated against the rule's (unchanged) check + stage
    so the effective rule stays runnable.
    """
    existing = await _get_rule_or_404(rule_id)
    if body.config is not None:
        _validate_rule(existing["check_id"], existing["stage"], dict(body.config))
    updated = await update_platform_rule(
        rule_id,
        config=body.config,
        priority=body.priority,
        description=body.description,
        enabled=body.enabled,
    )
    if updated is None:
        raise_error_response(
            "NOT_FOUND",
            "Platform safety rule not found.",
            status_code=404,
            details={"rule_id": rule_id},
        )
    logger.info(
        "platform safety rule updated",
        actor_user_id=user.user_id,
        rule_id=rule_id,
        before={"config": existing["config"], "enabled": existing["enabled"],
                "priority": existing["priority"]},
        after={"config": updated["config"], "enabled": updated["enabled"],
               "priority": updated["priority"]},
    )
    return _to_response(updated)


@router.post("/{rule_id}/enable", response_model=PlatformSafetyRule)
async def enable_platform_rule_endpoint(
    rule_id: str,
    user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> PlatformSafetyRule:
    """Enable a platform safety rule. Unknown id → 404."""
    await _get_rule_or_404(rule_id)
    updated = await set_platform_rule_enabled(rule_id, enabled=True)
    if updated is None:  # raced delete
        raise_error_response(
            "NOT_FOUND", "Platform safety rule not found.",
            status_code=404, details={"rule_id": rule_id},
        )
    logger.info(
        "platform safety rule enabled",
        actor_user_id=user.user_id, rule_id=rule_id,
    )
    return _to_response(updated)


@router.post("/{rule_id}/disable", response_model=PlatformSafetyRule)
async def disable_platform_rule_endpoint(
    rule_id: str,
    user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> PlatformSafetyRule:
    """Disable a platform safety rule. Also the sanctioned way to suppress a
    seeded default (which cannot be hard-deleted). Unknown id → 404."""
    await _get_rule_or_404(rule_id)
    updated = await set_platform_rule_enabled(rule_id, enabled=False)
    if updated is None:  # raced delete
        raise_error_response(
            "NOT_FOUND", "Platform safety rule not found.",
            status_code=404, details={"rule_id": rule_id},
        )
    logger.info(
        "platform safety rule disabled",
        actor_user_id=user.user_id, rule_id=rule_id,
    )
    return _to_response(updated)


@router.delete("/{rule_id}", status_code=204)
async def delete_platform_rule_endpoint(
    rule_id: str,
    user: AuthenticatedUser = Depends(require_action("safety.platform.manage")),
) -> Response:
    """Hard-delete a platform safety rule.

    A seeded factory default (``is_seeded=True``) cannot be deleted (409
    ``SEEDED_RULE_IMMUTABLE``) — the next build-time seed would resurrect it;
    disable it instead. Unknown id → 404.
    """
    existing = await _get_rule_or_404(rule_id)
    if existing["is_seeded"]:
        raise_error_response(
            "SEEDED_RULE_IMMUTABLE",
            (
                "Seeded factory-default platform rules cannot be deleted "
                "(the next seed would recreate them). Disable it instead."
            ),
            status_code=409,
            details={"rule_id": rule_id},
        )
    await delete_platform_rule(rule_id)
    logger.info(
        "platform safety rule deleted",
        actor_user_id=user.user_id, rule_id=rule_id,
        tier=existing["tier"], check_id=existing["check_id"],
        stage=existing["stage"],
    )
    return Response(status_code=204)
